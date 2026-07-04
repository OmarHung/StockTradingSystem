"""盤中即時停損監控（09:00–13:30，排程自動啟動）。

分工：進場決策留在盤後（每日主流程），出場保護搬進盤中——
持倉的停損/停利不再等收盤才檢查，盤中觸價立即出場，
極端下殺日不用多吃收盤前那段跌幅。

機制：每 30 秒對「持倉股票」抓 shioaji 快照（一次一呼叫，額度極寬），
用當日 low/high 對照停損/停利價（兩次輪詢之間的極值也抓得到）。
觸發 → PaperBroker.intraday_exit 立即平倉（冪等：收盤 check_stops
再跑到同檔也不會重複出場）。無持倉或收盤即自行結束。

用法：
    .venv/bin/python -m scripts.intraday_monitor            # 排程/手動皆可
    .venv/bin/python -m scripts.intraday_monitor --once     # 只檢查一輪（測試用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

from src.broker.paper import PaperBroker
from src.config import get_settings
from src.data import shioaji_source
from src.logging_setup import get_logger, setup_logging

log = get_logger("intraday")

MARKET_CLOSE = dt.time(13, 32)   # 收盤 13:30 + 緩衝
POLL_SEC = 30
# 快照日期連續 N 輪不是今天 → 今天休市，收工
STALE_LIMIT = 3


def run(once: bool = False) -> None:
    today = dt.date.today()
    if today.weekday() >= 5:
        log.info("週末休市，盤中監控結束")
        return
    if dt.datetime.now().time() >= MARKET_CLOSE and not once:
        log.info("已過收盤時間，盤中監控結束（收盤風控由每日主流程處理）")
        return
    if not shioaji_source.available():
        log.warning("shioaji 未設定（SJ_API_KEY/SJ_SEC_KEY），無法盤中監控")
        return

    broker = PaperBroker()
    stale_rounds = 0
    log.info("盤中停損監控啟動（每 %d 秒輪詢持倉快照，收盤自動結束）", POLL_SEC)

    while True:
        now = dt.datetime.now()
        if now.time() >= MARKET_CLOSE:
            log.info("收盤，盤中監控結束")
            return

        pos = broker.positions()
        if pos.empty:
            log.info("無持倉，盤中監控結束")
            return

        try:
            snaps = shioaji_source.fetch_snapshots(list(pos["stock_id"]))
        except Exception as e:  # noqa: BLE001
            log.warning("快照抓取失敗（下輪重試）：%s", str(e)[:100])
            snaps = {}

        today_iso = today.isoformat()
        if snaps and all(s.get("ts_date") != today_iso for s in snaps.values()):
            stale_rounds += 1
            if stale_rounds >= STALE_LIMIT:
                log.info("快照日期非今日（連續 %d 輪）→ 今天休市，盤中監控結束", stale_rounds)
                return
        else:
            stale_rounds = 0

        for p in pos.to_dict(orient="records"):
            s = snaps.get(p["stock_id"])
            if not s or s.get("ts_date") != today_iso:
                continue
            exit_price, reason = None, None
            # 與收盤 check_stops 同一優先序：停損優先於停利
            if p["stop_loss"] and s["low"] > 0 and s["low"] <= float(p["stop_loss"]):
                exit_price, reason = float(p["stop_loss"]), "stop_intraday"
            elif p["target"] and s["high"] > 0 and s["high"] >= float(p["target"]):
                exit_price, reason = float(p["target"]), "target_intraday"
            if exit_price is None:
                continue
            r = broker.intraday_exit(today_iso, p["stock_id"], exit_price, reason)
            if r:
                log.info("⚡ 盤中出場 %s %s @%.2f（%s，損益 %+.0f）",
                         r["stock_id"], reason, r["price"], f"{r['shares']}股", r["pnl"])

        if once:
            return
        time.sleep(POLL_SEC)


def main() -> None:
    ap = argparse.ArgumentParser(description="盤中即時停損監控")
    ap.add_argument("--once", action="store_true", help="只檢查一輪（測試用）")
    args = ap.parse_args()
    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_dir)
    run(once=args.once)


if __name__ == "__main__":
    main()
