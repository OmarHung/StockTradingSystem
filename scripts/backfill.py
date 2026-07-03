"""歷史資料回補主程式。

先抓股票清單 → 篩選股票池 → 對每檔增量回補 日K/法人/融資券/月營收。
增量：若 fetch_log 已有 last_date，則只從隔日續抓（冪等，可重跑）。

用法：
    # 全市場回補（依 settings.yaml 的 backfill_start 起）
    .venv/bin/python -m scripts.backfill

    # 只回補指定股票（測試用）
    .venv/bin/python -m scripts.backfill --stocks 2330 2317 --start 2024-01-01

    # 限制檔數（快速冒煙測試）
    .venv/bin/python -m scripts.backfill --limit 20
"""
from __future__ import annotations

import argparse
import datetime as dt

from tqdm import tqdm

from src.config import get_settings
from src.data import database as db
from src.data import fetchers
from src.data.finmind_client import FinMindClient
from src.logging_setup import get_logger, setup_logging

log = get_logger("backfill")

# dataset 名稱 -> 回補函式（月營收用 revenue 進度 key）
FETCHERS = {
    "price_daily": fetchers.fetch_price,
    "institutional": fetchers.fetch_institutional,
    "margin": fetchers.fetch_margin,
    "month_revenue": fetchers.fetch_month_revenue,
}


def _next_day(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str) + dt.timedelta(days=1)
    return d.isoformat()


def backfill(stocks: list[str] | None, start: str | None, end: str | None,
             limit: int | None, force: bool = False) -> None:
    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_dir)

    db.init_db(cfg.db_path)
    fm = cfg.finmind
    client = FinMindClient(
        base_url=fm["base_url"],
        token=cfg.finmind_token,
        request_interval_sec=fm["request_interval_sec"],
        max_retries=fm["max_retries"],
    )

    default_start = start or cfg.backfill_start
    end = end or dt.date.today().isoformat()

    with db.connect(cfg.db_path) as conn:
        # 1) 股票清單（每次回補都刷新）
        fetchers.fetch_stock_info(client, conn)

        # 2) 決定回補標的
        if stocks:
            targets = stocks
        else:
            targets = fetchers.select_universe(conn, cfg)
        if limit:
            targets = targets[:limit]

        log.info("開始回補 %d 檔，期間 %s ~ %s", len(targets), default_start, end)

        # 3) 逐檔逐 dataset 增量回補
        totals = {name: 0 for name in FETCHERS}
        for sid in tqdm(targets, desc="回補進度"):
            for name, fn in FETCHERS.items():
                last = None if force else db.get_last_date(conn, name, sid)
                s = _next_day(last) if last else default_start
                if s > end:
                    continue  # 已是最新
                try:
                    n = fn(client, conn, sid, s, end)
                    totals[name] += n
                except Exception as e:  # noqa: BLE001 — 單檔失敗不中斷整體回補
                    log.error("回補 %s/%s 失敗：%s", name, sid, e)
            conn.commit()

    log.info("回補完成，各 dataset 寫入列數：%s", totals)


def main() -> None:
    ap = argparse.ArgumentParser(description="台股歷史資料回補")
    ap.add_argument("--stocks", nargs="*", help="指定股票代號（預設全市場）")
    ap.add_argument("--start", help="起始日 YYYY-MM-DD（預設用 settings.yaml）")
    ap.add_argument("--end", help="結束日 YYYY-MM-DD（預設今天）")
    ap.add_argument("--limit", type=int, help="限制回補檔數（冒煙測試用）")
    ap.add_argument("--force", action="store_true",
                    help="忽略 fetch_log，全期重抓（用於回填缺口，upsert 會自動去重）")
    args = ap.parse_args()
    backfill(args.stocks, args.start, args.end, args.limit, args.force)


if __name__ == "__main__":
    main()
