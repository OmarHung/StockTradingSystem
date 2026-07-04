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
             limit: int | None, force: bool = False) -> None:
    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_dir)
    db.init_db(cfg.db_path)

    fm = cfg.finmind
    client = FinMindClient(
        base_url=fm["base_url"], token=cfg.finmind_token,
        request_interval_sec=fm["request_interval_sec"], max_retries=fm["max_retries"],
    )
    default_start = start or cfg.backfill_start
    end = end or dt.date.today().isoformat()

    with db.connect(cfg.db_path) as conn:
        fetchers.fetch_stock_info(client, conn)
        fetchers.fetch_disposition(conn)  # 官方處置股名單（全市場快照，免 token）
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

        log.info("開始回補 %d 檔，期間 %s ~ %s（%s）",
                 total, default_start, end, "強制重抓" if force else "最新優先")
        totals = {name: 0 for name in FETCHERS}
        finmind_dead = False  # 402 額度用罄 → 停止所有 FinMind 拉取

        for pass_label, gap_fn in passes:
            if finmind_dead:
                break
            for i, sid in enumerate(targets):
                if finmind_dead:
                    break
                rows_this = 0
                for name, fn in FETCHERS.items():
                    # 指數只有價格資料，其餘 dataset 跳過（省 API 額度）
                    if sid in INDEX_IDS and name != "price_daily":
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

        # FinMind 額度用罄 → 嘗試 shioaji 備援續補「股價日K」（按日全市場，額度效率高）
        if finmind_dead:
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


def _shioaji_price_backfill(conn, targets: list[str], start: str, end: str) -> None:
    """用 shioaji daily_quotes 按「交易日」補股價缺口（最新優先，一次一天全市場）。"""
    wanted = {t for t in targets if t not in INDEX_IDS}
    if not wanted:
        return
    # 候選日期：TAIEX 交易日曆；沒有日曆就用平日
    cal = [r[0] for r in conn.execute(
        "SELECT date FROM price_daily WHERE stock_id='TAIEX' AND date>=? AND date<=? ORDER BY date DESC",
        (start, end)).fetchall()]
    if not cal:
        d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        cal = [d.isoformat() for d in
               (d1 - dt.timedelta(days=i) for i in range((d1 - d0).days + 1))
               if d.weekday() < 5]

    placeholders = ",".join("?" * len(wanted))
    total = len(cal)
    filled = 0
    for i, day in enumerate(cal):  # 已是最新在前（最新優先）
        have = conn.execute(
            f"SELECT COUNT(DISTINCT stock_id) FROM price_daily "
            f"WHERE date=? AND stock_id IN ({placeholders})",
            (day, *wanted)).fetchone()[0]
        if have >= len(wanted):
            continue  # 該日已完整
        try:
            n = shioaji_source.fetch_daily_for_date(conn, day, wanted)
            filled += n
            conn.commit()
        except Exception as e:  # noqa: BLE001 — 單日失敗不中斷
            log.error("shioaji 補 %s 失敗：%s", day, e)
            n = 0
        _emit_progress({"pass": "備援(shioaji)", "current": i + 1, "total": total,
                        "stock_id": day, "rows": n})
    log.info("shioaji 備援補入 %d 列股價", filled)


def main() -> None:
    ap = argparse.ArgumentParser(description="台股歷史資料回補（最新優先）")
    ap.add_argument("--stocks", nargs="*", help="指定股票代號（預設全市場）")
    ap.add_argument("--start", help="起始日 YYYY-MM-DD（預設用 settings.yaml）")
    ap.add_argument("--end", help="結束日 YYYY-MM-DD（預設今天）")
    ap.add_argument("--limit", type=int, help="限制回補檔數")
    ap.add_argument("--force", action="store_true", help="忽略已補範圍，全期重抓")
    args = ap.parse_args()
    backfill(args.stocks, args.start, args.end, args.limit, args.force)


if __name__ == "__main__":
    main()
