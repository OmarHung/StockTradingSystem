"""歷史資料回補主程式（最新優先，逐檔進度）。

策略：**先補最新，再補歷史**，分兩趟外層迴圈：
  Pass 1「最新」：對所有股票補「最新缺口」(last_date+1 ~ end；首次則補最近一段)。
                  這趟跑完，每檔都有最近資料——即使 API 額度中途用罄也先可用。
  Pass 2「歷史」：對所有股票補「歷史缺口」(start ~ first_date-1)，把歷史補齊。

進度：每檔輸出一行 `@@PROGRESS@@ {json}`，供 WebUI 解析出「第幾檔/共幾檔/趟別/寫入列數」。
冪等：靠 upsert + fetch_log 的已補範圍 (first_date, last_date)；重跑安全。

用法：
    .venv/bin/python -m scripts.backfill                       # 全市場，最新優先
    .venv/bin/python -m scripts.backfill --stocks 2330 2317    # 指定股票
    .venv/bin/python -m scripts.backfill --limit 50            # 限制檔數
    .venv/bin/python -m scripts.backfill --force               # 忽略已補範圍全期重抓
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

from src.config import get_settings
from src.data import database as db
from src.data import fetchers
from src.data import shioaji_source
from src.data import twse_source
from src.data.finmind_client import FinMindClient, FinMindQuotaExhausted
from src.logging_setup import get_logger, setup_logging

log = get_logger("backfill")

# FinMind 抓取器（僅籌碼/基本面；K 線已全面改 shioaji，見 _shioaji_*_backfill）
FETCHERS = {
    "institutional": fetchers.fetch_institutional,
    "margin": fetchers.fetch_margin,
    "month_revenue": fetchers.fetch_month_revenue,
    "dividend": fetchers.fetch_dividend,     # 除權息（還原價計算必需）
}
DATASET_LABELS = {
    "price_daily": "股價日K", "institutional": "三大法人", "margin": "融資融券",
    "month_revenue": "月營收", "dividend": "除權息",
    "valuation": "估值(本益比/殖利率/淨值比)",   # 純官方源（BWIBBU_d/peQryDate），無 FinMind 備援
}

# 大盤指數（市場濾網/相對強弱/交易日曆的基準），一律納入回補；
# 由 shioaji 指數 1 分 K 聚合日 K（daily_quotes 不含指數）
INDEX_IDS = ["TAIEX", "TPEx"]
# 基準 ETF（回測 buy_and_hold/ma_cross 基準）：股票池排除 ETF，但基準價格必須有——
# 一律納入回補目標；shioaji 按日快照（daily_quotes）含 ETF，免額外呼叫
BENCH_IDS = ["0050"]

# 首次回補時，Pass 1「最新」先抓最近這麼多天（其餘留給 Pass 2 歷史）
INITIAL_WINDOW_DAYS = 400


def _shift(date_str: str, days: int) -> str:
    return (dt.date.fromisoformat(date_str) + dt.timedelta(days=days)).isoformat()


def _newest_gap(first: str | None, last: str | None, start: str, end: str) -> tuple[str, str] | None:
    """Pass 1 要補的『最新缺口』。"""
    if last is None:  # 首次：抓最近一段
        s = max(start, _shift(end, -INITIAL_WINDOW_DAYS))
        return (s, end)
    if last < end:  # 已有資料，補到最新
        return (_shift(last, 1), end)
    return None


def _history_gap(first: str | None, last: str | None, start: str, end: str) -> tuple[str, str] | None:
    """Pass 2 要補的『歷史缺口』。"""
    if last is None:  # 首次：Pass 1 抓了最近段，這裡補更早的
        newest_start = max(start, _shift(end, -INITIAL_WINDOW_DAYS))
        if newest_start > start:
            return (start, _shift(newest_start, -1))
        return None
    if first and first > start:  # 有資料，往前補歷史
        return (start, _shift(first, -1))
    return None


def _emit_progress(payload: dict) -> None:
    """輸出結構化進度行（WebUI 解析用）。"""
    print("@@PROGRESS@@ " + json.dumps(payload, ensure_ascii=False), flush=True)


def backfill(stocks: list[str] | None, start: str | None, end: str | None,
             limit: int | None, force: bool = False,
             datasets: list[str] | None = None,
             auto_wait: bool = False) -> None:
    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_dir)
    db.init_db(cfg.db_path)

    fm = cfg.finmind

    def _on_quota_wait(resume_at: str) -> None:
        # 讓 WebUI 進度顯示「等待額度中，HH:MM 恢復」
        _emit_progress({"pass": "等待額度", "current": 0, "total": 0,
                        "stock_id": resume_at, "rows": 0})

    client = FinMindClient(
        base_url=fm["base_url"], token=cfg.finmind_token,
        request_interval_sec=fm["request_interval_sec"], max_retries=fm["max_retries"],
        quota_wait=auto_wait, on_quota_wait=_on_quota_wait,
    )
    default_start = start or cfg.backfill_start
    end = end or dt.date.today().isoformat()

    # 要回補哪些資料類型（預設全部）；active = 其中走 FinMind 的子集
    requested = set(datasets) if datasets else set(DATASET_LABELS)
    active = {k: v for k, v in FETCHERS.items() if k in requested}
    if not active and not (requested & set(DATASET_LABELS)):
        log.error("無有效資料類型（可選：%s）", ",".join(DATASET_LABELS))
        return

    finmind_dead = False  # 402 額度用罄 → 停止所有 FinMind 拉取

    with db.connect(cfg.db_path) as conn:
        # 股票清單也走 FinMind——起手就 402 時不 crash，靠既有清單續跑
        # 各前置步驟之間逐一 commit：每步都夾著網路請求（FinMind/官方站/shioaji
        # 登入），未落地就進下一個網路呼叫會讓寫鎖橫跨數十秒 → API 行程撞
        # database is locked。原則：網路 I/O 前先把前一步的寫入提交。
        try:
            fetchers.fetch_stock_info(client, conn)
        except FinMindQuotaExhausted:
            log.warning("FinMind 額度用罄（402），無法刷新股票清單，改用資料庫既有清單")
            finmind_dead = True
        conn.commit()
        # 假日表（TWSE 官方年度休市日，一次呼叫）：委託撮合/排程判斷交易日用。
        # 每晚刷新一次順便涵蓋年度換檔與官方臨時公告。
        try:
            from src.data import market_calendar
            market_calendar.sync_holidays(conn)
        except Exception as e:  # noqa: BLE001
            log.warning("假日表同步失敗（不影響回補）：%s", str(e)[:100])
        conn.commit()
        fetchers.fetch_disposition(conn)  # 官方處置股名單（TWSE/TPEx，不受 FinMind 額度影響）
        conn.commit()
        if shioaji_source.available():    # 處置股第二源（雙源並用）
            try:
                shioaji_source.fetch_disposition(conn)
            except Exception as e:  # noqa: BLE001
                log.warning("shioaji 處置股抓取失敗（不影響回補）：%s", e)
            conn.commit()
        fetchers.mark_delisted(conn)      # 下市標記（券商合約為準，過濾殭屍代號）
        conn.commit()
        targets = stocks if stocks else fetchers.select_universe(conn, cfg)
        if limit:
            targets = targets[:limit]
        # 大盤指數一律優先回補（交易日曆/市場濾網基準）
        targets = INDEX_IDS + BENCH_IDS + [t for t in targets
                                           if t not in INDEX_IDS and t not in BENCH_IDS]

        total = len(targets)
        passes = [("最新", _newest_gap), ("歷史", _history_gap)]
        # force：不分趟，直接全期重抓
        if force:
            passes = [("全期", lambda f, l, s, e: (s, e))]

        log.info("開始回補 %d 檔，期間 %s ~ %s（%s），資料類型：%s",
                 total, default_start, end, "強制重抓" if force else "最新優先",
                 "、".join(DATASET_LABELS[k] for k in DATASET_LABELS if k in requested))
        totals = {name: 0 for name in active}

        # 股價唯一來源：shioaji（按交易日全市場、最新優先、流量制額度寬）。
        # FinMind 已完全退出 K 線路徑（額度全留給法人/營收/除權息）。
        if "price_daily" in requested:
            if shioaji_source.available():
                _shioaji_index_backfill(conn, default_start, end, force=force)
                # 限定股票/檔數的回補不可標記日期完成——sj_daily 標記是「全市場
                # 該日已補」的全域語義，部分回補標了會讓其他股票永遠缺那天
                _shioaji_price_backfill(conn, targets, default_start, end, force=force,
                                        mark_dates=not stocks and not limit)
            else:
                log.error("股價日K 需要 shioaji（.env 設 SJ_API_KEY / SJ_SEC_KEY）——"
                          "FinMind 已不再作為 K 線來源，本輪跳過股價回補")
        # 籌碼主源：TWSE/TPEx 官方（按交易日全市場，免額度）；除權息主源：TWT49U
        # 官方覆蓋完成的市場，FinMind 對應 dataset 自動跳過（降級為備援）
        official_skip: dict[str, set[str]] = {}   # dataset -> 要跳過的市場集合
        if {"institutional", "margin"} & set(active):
            covered = _official_chips_backfill(conn, default_start, end, force=force)
            for mkt, ok in covered.items():
                if ok:
                    official_skip.setdefault("institutional", set()).add(mkt)
                    official_skip.setdefault("margin", set()).add(mkt)
            if covered.get("twse") or covered.get("tpex"):
                log.info("法人/融資券官方源覆蓋完成：%s（FinMind 對應市場跳過）",
                         "、".join(m for m, ok in covered.items() if ok))
        if "valuation" in requested:
            _official_valuation_backfill(conn, default_start, end, force=force)
        if "dividend" in active:
            div_counts = _official_dividend_backfill(conn, default_start, end, force=force)
            for mkt, n in div_counts.items():
                if n >= 0:   # 官方源成功（含 0 筆＝該期間確實無除權息）→ FinMind 跳過
                    official_skip.setdefault("dividend", set()).add(mkt)
            if any(n > 0 for n in div_counts.values()):
                log.info("除權息官方源補入 上市 %s / 上櫃 %s 筆（成功市場 FinMind 跳過）",
                         div_counts["twse"], div_counts["tpex"])
        if "month_revenue" in active:
            rev_cov = _official_revenue_backfill(conn, default_start, end, force=force)
            for mkt, ok in rev_cov.items():
                if ok:
                    official_skip.setdefault("month_revenue", set()).add(mkt)
            if any(rev_cov.values()):
                log.info("月營收官方源（MOPS）覆蓋：%s（FinMind 對應市場跳過）",
                         "、".join(m for m, ok in rev_cov.items() if ok))

        # 股票 → 市場對照（官方源跳過判斷用）
        mkt_map = {r[0]: r[1] for r in conn.execute("SELECT stock_id, type FROM stock_info")}

        if not active:
            passes = []  # 純價格/估值更新：無 FinMind dataset，跳過逐檔迴圈
        for pass_label, gap_fn in passes:
            if finmind_dead:
                break
            for i, sid in enumerate(targets):
                if finmind_dead:
                    break
                rows_this = 0
                for name, fn in active.items():
                    # 指數只有價格資料（已由 shioaji 處理），FinMind dataset 全跳過
                    if sid in INDEX_IDS:
                        continue
                    # 官方源已覆蓋該市場的 dataset → FinMind 跳過（備援降級）
                    if name in official_skip and mkt_map.get(sid) in official_skip[name]:
                        continue
                    first, last = (None, None) if force else db.get_range(conn, name, sid)
                    seg = gap_fn(first, last, default_start, end)
                    if not seg or seg[0] > seg[1]:
                        continue
                    try:
                        n = fn(client, conn, sid, seg[0], seg[1])
                        rows_this += n
                        totals[name] += n
                    except FinMindQuotaExhausted:
                        log.warning("FinMind 額度用罄（402），停止 FinMind 拉取")
                        finmind_dead = True
                        break
                    except Exception as e:  # noqa: BLE001 — 單檔失敗不中斷
                        log.error("回補 %s/%s 失敗：%s", name, sid, e)
                conn.commit()
                _emit_progress({
                    "pass": pass_label, "current": i + 1, "total": total,
                    "stock_id": sid, "rows": rows_this,
                })

        # 公司行動偵測（分割/減資跳空 → 還原價事件）：務必在所有除權息資料
        # （官方 TWT49U/exDailyQ ＋ FinMind 備援）都入庫後才跑，否則除息跳空
        # 會被誤判成減資寫入 capital_change，與稍後補進的 dividend 同日事件疊乘、
        # 造成還原價雙重調整（見 query._apply_adjustment 的同日去重防護）。
        if "price_daily" in requested or "dividend" in active:
            from src.data import corporate_actions
            n_ca = corporate_actions.detect(conn)
            if n_ca:
                log.info("公司行動偵測：新增 %d 個價格調整事件（分割/減資）", n_ca)

    log.info("回補完成，各 dataset 寫入列數：%s", totals)
    _emit_progress({"pass": "完成", "current": total, "total": total, "stock_id": "", "rows": 0})


def _weekdays_desc(start: str, end: str) -> list[str]:
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return [d.isoformat() for d in
            (d1 - dt.timedelta(days=i) for i in range((d1 - d0).days + 1))
            if d.weekday() < 5]


def _trading_dates_desc(conn, start: str, end: str) -> list[str]:
    """TAIEX 交易日（新到舊）；無日曆退回平日。

    指數若因故停更，日曆末端之後的平日必須照樣納入候選，否則 shioaji/官方源
    全被舊日曆卡住，K 線永遠停在指數最後更新日。非交易日抓到 0 列會被各源
    正確標記/跳過，多試無害。
    """
    cal = [r[0] for r in conn.execute(
        "SELECT date FROM price_daily WHERE stock_id='TAIEX' AND date>=? AND date<=? ORDER BY date DESC",
        (start, end)).fetchall()]
    if not cal:
        return _weekdays_desc(start, end)
    if cal[0] < end:
        cal = _weekdays_desc(_shift(cal[0], 1), end) + cal
    return cal


def _official_chips_backfill(conn, start: str, end: str, force: bool = False) -> dict:
    """TWSE/TPEx 官方按日補 法人+融資券（最新優先，逐日逐市場標記，中斷安全）。

    回傳 {"twse": 是否全期覆蓋完成, "tpex": ...}——覆蓋完成的市場，
    FinMind 對應 dataset 直接跳過（官方一天 2 次呼叫 vs FinMind 逐檔數千次）。
    """
    today = dt.date.today().isoformat()
    cal = _trading_dates_desc(conn, start, end)
    if not cal:
        return {"twse": False, "tpex": False}

    done = {"twse": set(), "tpex": set()}
    if not force:
        for mkt in done:
            done[mkt] = {r[0] for r in conn.execute(
                "SELECT stock_id FROM fetch_log WHERE dataset=?",
                (f"{mkt}_chips_daily",)).fetchall()}

    todo = [d for d in cal if d not in done["twse"] or d not in done["tpex"]]
    if not todo:
        log.info("官方籌碼源已是最新（無缺日）")
        return {"twse": True, "tpex": True}

    now = dt.datetime.now().isoformat(timespec="seconds")
    total = len(todo)
    # 斷路器：某市場連續失敗 3 次 → 本輪跳過該市場（站掛了，別浪費逾時等待；
    # 未標記的日子下次執行自動續補）
    consec_fail = {"twse": 0, "tpex": 0}
    tripped: set[str] = set()

    def _fetch(mkt: str, fn, day: str) -> int:
        """單源抓取：錯誤隔離（一源失敗不影響其他源）+ 斷路器計數。"""
        if mkt in tripped:
            return 0
        try:
            n = fn(conn, day)
            consec_fail[mkt] = 0
            return n
        except Exception as e:  # noqa: BLE001
            consec_fail[mkt] += 1
            log.warning("官方籌碼 %s/%s %s 失敗：%s", mkt, fn.__name__, day, str(e)[:90])
            if consec_fail[mkt] >= 3:
                tripped.add(mkt)
                log.warning("官方源 %s 連續失敗 3 次，本輪停用（缺日下次自動續補）", mkt)
            return 0

    for i, day in enumerate(todo):
        need_twse = day not in done["twse"] and "twse" not in tripped
        need_tpex = day not in done["tpex"] and "tpex" not in tripped
        if not need_twse and not need_tpex:
            if tripped >= {"twse", "tpex"}:
                break  # 兩市場都熔斷，本輪結束
            continue
        counts = {"twse_inst": 0, "tpex_inst": 0, "twse_margin": 0, "tpex_margin": 0}
        if need_twse:
            counts["twse_inst"] = _fetch("twse", twse_source.fetch_twse_institutional, day)
            counts["twse_margin"] = _fetch("twse", twse_source.fetch_twse_margin, day)
        if need_tpex:
            counts["tpex_inst"] = _fetch("tpex", twse_source.fetch_tpex_institutional, day)
            counts["tpex_margin"] = _fetch("tpex", twse_source.fetch_tpex_margin, day)
        rows_day = sum(counts.values())
        # 逐市場標記完成（當日資料 ~16:30 才公布 → 今天 0 列不標記，明天再補）
        if need_twse and counts["twse_inst"] > 0 and counts["twse_margin"] > 0:
            db.merge_range(conn, "twse_chips_daily", day, day, day, now)
            done["twse"].add(day)
        if need_tpex and counts["tpex_inst"] > 0 and counts["tpex_margin"] > 0:
            db.merge_range(conn, "tpex_chips_daily", day, day, day, now)
            done["tpex"].add(day)
        conn.commit()
        _emit_progress({"pass": "籌碼(官方)", "current": i + 1, "total": total,
                        "stock_id": day, "rows": rows_day})

    cal_set = set(cal)
    coverage = {mkt: cal_set - {today} <= done[mkt] for mkt in ("twse", "tpex")}
    return coverage


def _official_valuation_backfill(conn, start: str, end: str, force: bool = False) -> None:
    """TWSE BWIBBU_d / TPEx peQryDate 按日補估值（本益比/殖利率/股價淨值比）。

    與籌碼迴圈同模式：最新優先、逐日逐市場標記（twse_val_daily/tpex_val_daily）、
    熔斷保護。純官方源，無 FinMind 備援（FinMind 免費層無此資料集）。
    """
    today = dt.date.today().isoformat()
    cal = _trading_dates_desc(conn, start, end)
    if not cal:
        return

    done = {"twse": set(), "tpex": set()}
    if not force:
        for mkt in done:
            done[mkt] = {r[0] for r in conn.execute(
                "SELECT stock_id FROM fetch_log WHERE dataset=?",
                (f"{mkt}_val_daily",)).fetchall()}

    todo = [d for d in cal if d not in done["twse"] or d not in done["tpex"]]
    if not todo:
        log.info("估值官方源已是最新（無缺日）")
        return

    now = dt.datetime.now().isoformat(timespec="seconds")
    total = len(todo)
    consec_fail = {"twse": 0, "tpex": 0}
    tripped: set[str] = set()

    def _fetch(mkt: str, fn, day: str) -> int:
        if mkt in tripped:
            return 0
        try:
            n = fn(conn, day)
            consec_fail[mkt] = 0
            return n
        except Exception as e:  # noqa: BLE001
            consec_fail[mkt] += 1
            log.warning("估值 %s %s 失敗：%s", mkt, day, str(e)[:90])
            if consec_fail[mkt] >= 3:
                tripped.add(mkt)
                log.warning("估值源 %s 連續失敗 3 次，本輪停用（缺日下次自動續補）", mkt)
            return 0

    for i, day in enumerate(todo):
        need_twse = day not in done["twse"] and "twse" not in tripped
        need_tpex = day not in done["tpex"] and "tpex" not in tripped
        if not need_twse and not need_tpex:
            if tripped >= {"twse", "tpex"}:
                break
            continue
        n_twse = _fetch("twse", twse_source.fetch_twse_valuation, day) if need_twse else 0
        n_tpex = _fetch("tpex", twse_source.fetch_tpex_valuation, day) if need_tpex else 0
        if need_twse and n_twse > 0:
            db.merge_range(conn, "twse_val_daily", day, day, day, now)
            done["twse"].add(day)
        if need_tpex and n_tpex > 0:
            db.merge_range(conn, "tpex_val_daily", day, day, day, now)
            done["tpex"].add(day)
        conn.commit()
        _emit_progress({"pass": "估值(官方)", "current": i + 1, "total": total,
                        "stock_id": day, "rows": n_twse + n_tpex})


def _official_revenue_backfill(conn, start: str, end: str, force: bool = False) -> dict:
    """MOPS 月營收（一市場一月一檔，全市場）。最新月份優先，逐月逐市場標記。

    當月營收於次月 10 日前公告：太新的月份抓到 0 檔不標記（下次再試）；
    超過 3 個月的舊月份抓不到則標記放棄（避免永遠重試不存在的檔案）。
    回傳 {"twse": 覆蓋完成, "tpex": ...}（twse↔sii、tpex↔otc）。
    """
    # 需要的營收月份 = start 前一個月 ~ end 前一個月（當月營收尚未公告）
    s = dt.date.fromisoformat(start).replace(day=1)
    e = dt.date.fromisoformat(end).replace(day=1) - dt.timedelta(days=1)
    e = e.replace(day=1)
    months = []
    cur = e
    while cur >= s:  # 新到舊（最新優先）
        months.append((cur.year, cur.month))
        cur = (cur - dt.timedelta(days=1)).replace(day=1)
    if not months:
        return {"twse": False, "tpex": False}

    mkt_map = {"twse": "sii", "tpex": "otc"}
    today = dt.date.today()

    # 自愈：清掉「公告期內就被標記完成」的舊月份標記（歷史 bug：fetch_mops_revenue
    # 內層曾無條件標記，遲公告公司營收因此被永久跳過）。以標記當下該月是否已達
    # fully_announced 門檻回推，未達者刪除讓本輪重補；已足月的正確標記保留。
    for mops in ("sii", "otc"):
        for key, upd in conn.execute(
                "SELECT stock_id, updated_at FROM fetch_log WHERE dataset=?",
                (f"mops_{mops}",)).fetchall():
            try:
                ky, km = int(key[:4]), int(key[5:7])
                t = dt.date.fromisoformat((upd or "")[:10])
            except (ValueError, TypeError, IndexError):
                continue
            old_at_mark = (t.year - ky) * 12 + (t.month - km)
            premature = old_at_mark < 1 or (old_at_mark == 1 and t.day <= 15)
            if premature:
                conn.execute("DELETE FROM fetch_log WHERE dataset=? AND stock_id=?",
                             (f"mops_{mops}", key))

    done: dict[str, set] = {}
    for mkt, mops in mkt_map.items():
        done[mkt] = set() if force else {
            r[0] for r in conn.execute(
                "SELECT stock_id FROM fetch_log WHERE dataset=?", (f"mops_{mops}",)).fetchall()}


    now = dt.datetime.now().isoformat(timespec="seconds")
    total = len(months)
    for i, (y, m) in enumerate(months):
        key = f"{y:04d}-{m:02d}"
        rows_month = 0
        for mkt, mops in mkt_map.items():
            if key in done[mkt]:
                continue
            try:
                n = twse_source.fetch_mops_revenue(conn, y, m, mops)
            except Exception as ex:  # noqa: BLE001
                log.warning("MOPS %s %s 失敗：%s", mops, key, str(ex)[:80])
                continue
            rows_month += n
            months_old = (today.year - y) * 12 + (today.month - m)
            # 標記完成的條件：
            # - 公告期已過（前月且已過 15 日，或更舊）且有抓到 → 完成
            # - 太舊仍抓不到（>3 月）→ 放棄標記，避免永遠重試
            # 注意：公告期內（上月10日前）只會抓到部分公司，不可標記
            fully_announced = months_old >= 2 or (months_old == 1 and today.day > 15)
            if (n > 0 and fully_announced) or (n == 0 and months_old > 3):
                db.merge_range(conn, f"mops_{mops}", key, key, key, now)
                done[mkt].add(key)
        if rows_month or any(key not in done[mkt] for mkt in done):
            conn.commit()
            _emit_progress({"pass": "月營收(MOPS)", "current": i + 1, "total": total,
                            "stock_id": key, "rows": rows_month})

    all_keys = {f"{y:04d}-{m:02d}" for y, m in months}
    # 最新一個月可能未公告：覆蓋判定容忍缺最新月
    latest_key = f"{months[0][0]:04d}-{months[0][1]:02d}"
    return {mkt: (all_keys - {latest_key}) <= done[mkt] for mkt in mkt_map}


def _quarter_segments(start: str, end: str) -> list[tuple[dt.date, dt.date]]:
    """把 [start, end] 切成逐季 (季初, 季末) 清單（新到舊，最新季優先）。"""
    s = dt.date.fromisoformat(start)
    segs = []
    cur = dt.date(dt.date.fromisoformat(end).year,
                  ((dt.date.fromisoformat(end).month - 1) // 3) * 3 + 1, 1)
    floor = dt.date(s.year, ((s.month - 1) // 3) * 3 + 1, 1)
    while cur >= floor:
        q_end_month = ((cur.month - 1) // 3 + 1) * 3
        q_end = (dt.date(cur.year, 12, 31) if q_end_month == 12
                 else dt.date(cur.year, q_end_month + 1, 1) - dt.timedelta(days=1))
        segs.append((cur, q_end))
        prev = cur - dt.timedelta(days=1)
        cur = dt.date(prev.year, ((prev.month - 1) // 3) * 3 + 1, 1)
    return segs


def _official_dividend_backfill(conn, start: str, end: str, force: bool = False) -> dict:
    """官方除權息（上市 TWT49U + 上櫃 exDailyQ，皆為日期區間全市場，逐季分段）。

    回傳 {"twse": 筆數, "tpex": 筆數}；成功的市場 FinMind 直接跳過。
    單市場失敗不中斷另一市場（各自 try，失敗市場筆數 -1 表示不可標記跳過）。

    逐季 fetch_log 標記（dataset=div_{mkt}, stock_id='YYYY-Qn'）：已結束的季抓過
    即標記、之後跳過，只有含今天的當季每次重抓——除權息計算結果是歷史真值、過去
    的季不會再變，避免每晚從 backfill_start 全期重抓（原本每晚重演數分鐘長寫鎖）。
    每段抓完立即 commit：後續段落的網路請求（節流 3~6 秒、數十次）不持有 SQLite
    寫鎖，否則長交易會讓 API 行程的帳本/UI 寫入撞 database is locked。
    """
    today = dt.date.today()
    counts = {"twse": 0, "tpex": 0}
    fns = {"twse": twse_source.fetch_dividends_range,
           "tpex": twse_source.fetch_tpex_dividends_range}
    lo, hi = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    for mkt, fn in fns.items():
        dataset = f"div_{mkt}"
        done = set() if force else {
            r[0] for r in conn.execute(
                "SELECT stock_id FROM fetch_log WHERE dataset=?", (dataset,)).fetchall()}
        now = dt.datetime.now().isoformat(timespec="seconds")
        try:
            for q_start, q_end in _quarter_segments(start, end):
                key = f"{q_start.year}-Q{(q_start.month - 1) // 3 + 1}"
                elapsed = q_end < today          # 整季已結束（歷史真值穩定）
                if elapsed and key in done:
                    continue
                counts[mkt] += fn(conn, max(q_start, lo).isoformat(),
                                  min(q_end, hi).isoformat())
                if elapsed:
                    db.merge_range(conn, dataset, key, key, key, now)
                conn.commit()                    # 每段落地：下一段網路請求前不持寫鎖
        except Exception as ex:  # noqa: BLE001
            conn.commit()                        # 已抓段落保留，不整批回滾
            log.warning("除權息官方源 %s 失敗（該市場維持 FinMind）：%s", mkt, str(ex)[:100])
            counts[mkt] = -1
    # 除權息預告（未來日程快照，供決策避開/預期即將除權息標的）
    try:
        n_fc = twse_source.fetch_dividend_forecast(conn)
        conn.commit()
        if n_fc:
            log.info("除權息預告更新 %d 筆", n_fc)
    except Exception as ex:  # noqa: BLE001
        log.warning("除權息預告更新失敗：%s", str(ex)[:100])
    return counts


def _shioaji_index_backfill(conn, start: str, end: str, force: bool = False) -> None:
    """指數日 K（TAIEX/TPEx）：shioaji 指數 1 分 K 聚合。增量：從已補範圍的
    「最後一天」重抓到今天——最後一天可能是盤中寫入的不完整日 K，重抓覆蓋。"""
    seg_start = start
    if not force:
        lasts = [db.get_range(conn, "price_daily", sid)[1] for sid in INDEX_IDS]
        if all(lasts):
            seg_start = min(lasts)   # 兩指數一起補，取較舊者
        if seg_start > end:
            return
    try:
        n = shioaji_source.fetch_index_daily(conn, seg_start, end)
        if n:
            log.info("指數日K（shioaji）補入 %d 列（%s ~ %s）", n, seg_start, end)
    except Exception as e:  # noqa: BLE001 — 指數失敗不中斷個股回補
        log.error("指數日K 回補失敗：%s", e)
    _emit_progress({"pass": "指數(shioaji)", "current": 1, "total": 1,
                    "stock_id": "TAIEX/TPEx", "rows": 0})


def _shioaji_price_backfill(conn, targets: list[str], start: str, end: str,
                            force: bool = False, mark_dates: bool = True) -> None:
    """用 shioaji daily_quotes 按「交易日」補股價（最新優先，一次一天全市場）。

    完成進度以 fetch_log(dataset='sj_daily', stock_id=<日期>) 逐日標記——
    中斷安全（重跑只補沒標記的日子），不會產生範圍端點誤蓋缺口的問題。
    休市日（0 列）也標記完成避免每次重掃；「今天且 0 列」不標記（盤後再補）。
    mark_dates=False（限定股票/檔數的部分回補）：只寫資料不標記，
    因為標記語義是「全市場該日已補」。
    """
    wanted = {t for t in targets if t not in INDEX_IDS}
    if not wanted:
        return
    today = dt.date.today().isoformat()

    # 候選日期：TAIEX 交易日曆（新到舊；含日曆停更後的平日，見 _trading_dates_desc）
    cal = _trading_dates_desc(conn, start, end)

    done: set[str] = set() if force else {
        r[0] for r in conn.execute(
            "SELECT stock_id FROM fetch_log WHERE dataset='sj_daily'").fetchall()
    }
    todo = [d for d in cal if d not in done]
    if not todo:
        log.info("shioaji 股價已是最新（無缺日）")
        return

    now = dt.datetime.now().isoformat(timespec="seconds")
    total, filled = len(todo), 0
    for i, day in enumerate(todo):  # 新到舊（最新優先）
        try:
            n = shioaji_source.fetch_daily_for_date(conn, day, wanted)
        except Exception as e:  # noqa: BLE001 — 單日失敗不中斷、不標記完成
            log.error("shioaji 補 %s 失敗：%s", day, e)
            _emit_progress({"pass": "股價(shioaji)", "current": i + 1, "total": total,
                            "stock_id": day, "rows": 0})
            continue
        filled += n
        if mark_dates and (n > 0 or day < today):  # 今天還沒收盤可能 0 列 → 不標記，下次再試
            db.merge_range(conn, "sj_daily", day, day, day, now)
        conn.commit()
        _emit_progress({"pass": "股價(shioaji)", "current": i + 1, "total": total,
                        "stock_id": day, "rows": n})
    log.info("shioaji 股價補入 %d 列（%d 個交易日）", filled, total)


def main() -> None:
    ap = argparse.ArgumentParser(description="台股歷史資料回補（最新優先）")
    ap.add_argument("--stocks", nargs="*", help="指定股票代號（預設全市場）")
    ap.add_argument("--start", help="起始日 YYYY-MM-DD（預設用 settings.yaml）")
    ap.add_argument("--end", help="結束日 YYYY-MM-DD（預設今天）")
    ap.add_argument("--limit", type=int, help="限制回補檔數")
    ap.add_argument("--force", action="store_true", help="忽略已補範圍，全期重抓")
    ap.add_argument("--datasets", help=f"逗號分隔的資料類型（預設全部）：{','.join(DATASET_LABELS)}")
    ap.add_argument("--auto-wait", action="store_true",
                    help="FinMind 額度用罄時自動等到下個整點續跑（背景/過夜更新用）")
    args = ap.parse_args()
    ds = [s.strip() for s in args.datasets.split(",")] if args.datasets else None
    backfill(args.stocks, args.start, args.end, args.limit, args.force, ds, args.auto_wait)


if __name__ == "__main__":
    main()
