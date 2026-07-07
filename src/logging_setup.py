"""統一 logging 設定：同時輸出到 console 與 logs/ 檔案（含輪替與未捕捉例外）。"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

_CONFIGURED = False

# 第三方套件雜訊：降到 WARNING 以上才記
_NOISY_LOGGERS = ["urllib3", "httpx", "httpcore", "anthropic", "asyncio", "watchfiles"]


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> None:
    """設定 root logger（重複呼叫僅生效一次）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 輪替：單檔 10MB、保留 5 份，避免 VPS 上無限膨脹
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "system.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # warnings.warn() 與未捕捉例外也進 log 檔（否則只出現在 stderr，檔案裡看不到）
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught

    _CONFIGURED = True


def _log_uncaught(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("uncaught").critical("未捕捉例外", exc_info=(exc_type, exc, tb))


def _log_thread_uncaught(args):
    logging.getLogger("uncaught").critical(
        "執行緒 %s 未捕捉例外", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
