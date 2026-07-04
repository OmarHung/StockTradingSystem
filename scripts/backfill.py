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
from src.data.finmind_client import FinMindClient
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

        for pass_label, gap_fn in passes:
            for i, sid in enumerate(targets):
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
                    except Exception as e:  # noqa: BLE001 — 單檔失敗不中斷
                        log.error("回補 %s/%s 失敗：%s", name, sid, e)
                conn.commit()
                _emit_progress({
                    "pass": pass_label, "current": i + 1, "total": total,
                    "stock_id": sid, "rows": rows_this,
                })

    log.info("回補完成，各 dataset 寫入列數：%s", totals)
    _emit_progress({"pass": "完成", "current": total, "total": total, "stock_id": "", "rows": 0})


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
