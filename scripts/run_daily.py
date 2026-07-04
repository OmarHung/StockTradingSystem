"""每日主流程 CLI（供排程/WebUI 背景執行）。

用法：
    .venv/bin/python -m scripts.run_daily                    # 今天
    .venv/bin/python -m scripts.run_daily --as-of 2026-07-03 # 指定日期
    .venv/bin/python -m scripts.run_daily --no-decide        # 只做撮合/風控/快照

排程（macOS launchd 範例見 docs 或 README）：平日 15:00 盤後執行。
"""
from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from src.config import ROOT, get_settings
from src.logging_setup import setup_logging


def main() -> None:
    load_dotenv(ROOT / ".env", override=True)
    import os
    if not (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip():
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_dir)

    ap = argparse.ArgumentParser(description="每日交易主流程")
    ap.add_argument("--as-of", help="執行日期（預設今天）")
    ap.add_argument("--top-n", type=int, default=3, help="盤後決策檔數")
    ap.add_argument("--no-decide", action="store_true", help="跳過決策（只撮合/風控/快照）")
    args = ap.parse_args()

    from src.pipeline.daily import run_daily
    summary = run_daily(as_of=args.as_of, top_n=args.top_n, decide=not args.no_decide)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
