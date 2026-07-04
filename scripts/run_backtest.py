"""回測 CLI。

用法：
    .venv/bin/python -m scripts.run_backtest --strategy screener --start 2022-06-01 --end 2025-06-30
    .venv/bin/python -m scripts.run_backtest --strategy buy_and_hold
"""
from __future__ import annotations

import argparse
import json

from src.backtest.runner import run_backtest
from src.config import get_settings
from src.logging_setup import setup_logging


def main() -> None:
    ap = argparse.ArgumentParser(description="策略回測")
    ap.add_argument("--strategy", default="screener",
                    choices=["screener", "screener_risk", "buy_and_hold", "ma_cross"])
    ap.add_argument("--start", default="2022-06-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--cash", type=float, default=None)
    ap.add_argument("--max-positions", type=int, default=10)
    args = ap.parse_args()

    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_dir)

    res, m = run_backtest(
        args.strategy, args.start, args.end,
        initial_cash=args.cash, max_positions=args.max_positions,
    )
    print(json.dumps(m, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
