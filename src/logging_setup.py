"""統一 logging 設定：同時輸出到 console 與 logs/ 檔案。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> None:
    """設定 root logger（重複呼叫僅生效一次）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_dir / "system.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # 降低第三方套件雜訊
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
