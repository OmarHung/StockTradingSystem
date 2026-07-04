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

FETCHERS = {
    "price_daily": fetchers.fetch_price,
    "institutional": fetchers.fetch_institutional,
    "margin": fetchers.fetch_margin,
    "month_revenue": fetchers.fetch_month_revenue,
    "dividend": fetchers.fetch_dividend,     # 除權息（還原價計算必需）
}
DATASET_LABELS = {
    "price_daily": "股價日K", "institutional": "三大法人", "margin": "融資融券",
    "month_revenue": "月營收", "dividend": "除權息",
}

# 大盤指數（市場濾網/相對強弱/交易日曆的基準），一律納入回補
INDEX_IDS = ["TAIEX", "TPEx"]

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

    # 要回補哪些資料類型（預設全部）
    active = {k: v for k, v in FETCHERS.items() if not datasets or k in datasets}
    if not active:
        log.error("無有效資料類型（可選：%s）", ",".join(FETCHERS))
        return

    finmind_dead = False  # 402 額度用罄 → 停止所有 FinMind 拉取

    with db.connect(cfg.db_path) as conn:
        # 股票清單也走 FinMind——起手就 402 時不 crash，靠既有清單續跑
        try:
            fetchers.fetch_stock_info(client, conn)
        except FinMindQuotaExhausted:
            log.warning("FinMind 額度用罄（402），無法刷新股票清單，改用資料庫既有清單")
            finmind_dead = True
        fetchers.fetch_disposition(conn)  # 官方處置股名單（TWSE/TPEx，不受 FinMind 額度影響）
        if shioaji_source.available():    # 處置股第二源（雙源並用）
            try:
                shioaji_source.fetch_disposition(conn)
            except Exception as e:  # noqa: BLE001
                log.warning("shioaji 處置股抓取失敗（不影響回補）：%s", e)
        targets = stocks if stocks else fetchers.select_universe(conn, cfg)
        if limit:
            targets = targets[:limit]
        # 大盤指數一律優先回補（交易日曆/市場濾網基準）
        targets = INDEX_IDS + [t for t in targets if t not in INDEX_IDS]

        total = len(targets)
        passes = [("最新", _newest_gap), ("歷史", _history_gap)]
        # force：不分趟，直接全期重抓
        if force:
            passes = [("全期", lambda f, l, s, e: (s, e))]

        log.info("開始回補 %d 檔，期間 %s ~ %s（%s），資料類型：%s",
                 total, default_start, end, "強制重抓" if force else "最新優先",
                 "、".join(DATASET_LABELS[k] for k in active))
        totals = {name: 0 for name in active}

        # 股價主源：shioaji（按交易日全市場、最新優先、流量制額度寬）；
        # FinMind 只補指數價格與其餘資料集（額度全留給法人/營收/除權息）。
        sj_primary = "price_daily" in active and shioaji_source.available()
        if sj_primary:
            log.info("股價日K 主源＝shioaji（FinMind 額度保留給籌碼/基本面資料）")
            _shioaji_price_backfill(conn, targets, default_start, end, force=force)

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
        if "dividend" in active:
            try:
                n = _official_dividend_backfill(conn, default_start, end)
                if n > 0:
                    official_skip.setdefault("dividend", set()).add("twse")
                    log.info("除權息官方源（TWT49U）補入 %d 筆（上市檔 FinMind 跳過）", n)
            except Exception as e:  # noqa: BLE001
                log.warning("除權息官方源失敗，維持 FinMind：%s", e)

        # 股票 → 市場對照（官方源跳過判斷用）
        mkt_map = {r[0]: r[1] for r in conn.execute("SELECT stock_id, type FROM stock_info")}

        for pass_label, gap_fn in passes:
            if finmind_dead:
                break
            for i, sid in enumerate(targets):
                if finmind_dead:
                    break
                rows_this = 0
                for name, fn in active.items():
                    # 指數只有價格資料，其餘 dataset 跳過（省 API 額度）
                    if sid in INDEX_IDS and name != "price_daily":
                        continue
                    # 股價主源=shioaji 時，個股價格不再走 FinMind（指數仍走 FinMind，
                    # 因 daily_quotes 不含 TAIEX/TPEx）
                    if sj_primary and name == "price_daily" and sid not in INDEX_IDS:
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

        # FinMind 額度用罄 → 若股價尚未由 shioaji 主源處理過，嘗試備援續補
        if finmind_dead and "price_daily" in active and not sj_primary:
            if shioaji_source.available():
                log.info("改用 shioaji 備援續補股價日K（按交易日、最新優先）")
                _shioaji_price_backfill(conn, targets, default_start, end)
            else:
                log.warning(
                    "shioaji 備援未設定（.env 需 SJ_API_KEY / SJ_SEC_KEY）。"
                    "已停止回補，FinMind 額度重置後再續補即可（進度已保存）。"
                )

    log.info("回補完成，各 dataset 寫入列數：%s", totals)
    _emit_progress({"pass": "完成", "current": total, "total": total, "stock_id": "", "rows": 0})


def _trading_dates_desc(conn, start: str, end: str) -> list[str]:
    """TAIEX 交易日（新到舊）；無日曆退回平日。"""
    cal = [r[0] for r in conn.execute(
        "SELECT date FROM price_daily WHERE stock_id='TAIEX' AND date>=? AND date<=? ORDER BY date DESC",
        (start, end)).fetchall()]
    if cal:
        return cal
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return [d.isoformat() for d in
            (d1 - dt.timedelta(days=i) for i in range((d1 - d0).days + 1))
            if d.weekday() < 5]


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


def _official_dividend_backfill(conn, start: str, end: str) -> int:
    """TWT49U 除權息（日期區間全市場，按季分段避免單次過大）。"""
    total = 0
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    while s <= e:
        seg_end = min(s + dt.timedelta(days=92), e)
        total += twse_source.fetch_dividends_range(conn, s.isoformat(), seg_end.isoformat())
        s = seg_end + dt.timedelta(days=1)
    conn.commit()
    return total


def _shioaji_price_backfill(conn, targets: list[str], start: str, end: str,
                            force: bool = False) -> None:
    """用 shioaji daily_quotes 按「交易日」補股價（最新優先，一次一天全市場）。

    完成進度以 fetch_log(dataset='sj_daily', stock_id=<日期>) 逐日標記——
    中斷安全（重跑只補沒標記的日子），不會產生範圍端點誤蓋缺口的問題。
    休市日（0 列）也標記完成避免每次重掃；「今天且 0 列」不標記（盤後再補）。
    """
    wanted = {t for t in targets if t not in INDEX_IDS}
    if not wanted:
        return
    today = dt.date.today().isoformat()

    # 候選日期：TAIEX 交易日曆（新到舊）；沒有日曆就用平日
    cal = [r[0] for r in conn.execute(
        "SELECT date FROM price_daily WHERE stock_id='TAIEX' AND date>=? AND date<=? ORDER BY date DESC",
        (start, end)).fetchall()]
    if not cal:
        d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        cal = [d.isoformat() for d in
               (d1 - dt.timedelta(days=i) for i in range((d1 - d0).days + 1))
               if d.weekday() < 5]

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
        if n > 0 or day < today:  # 今天還沒收盤可能 0 列 → 不標記，下次再試
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
    ap.add_argument("--datasets", help=f"逗號分隔的資料類型（預設全部）：{','.join(FETCHERS)}")
    ap.add_argument("--auto-wait", action="store_true",
                    help="FinMind 額度用罄時自動等到下個整點續跑（背景/過夜更新用）")
    args = ap.parse_args()
    ds = [s.strip() for s in args.datasets.split(",")] if args.datasets else None
    backfill(args.stocks, args.start, args.end, args.limit, args.force, ds, args.auto_wait)


if __name__ == "__main__":
    main()
