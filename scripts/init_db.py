"""建立資料庫與所有資料表。

用法：
    .venv/bin/python -m scripts.init_db
"""
from __future__ import annotations

from src.config import get_settings
from src.data import database as db
from src.logging_setup import get_logger, setup_logging


def main() -> None:
    cfg = get_settings()
    setup_logging(cfg.log_level, cfg.log_dir)
    log = get_logger("init_db")
    db.init_db(cfg.db_path)
    log.info("資料庫已初始化：%s", cfg.db_path)
    log.info("已建立資料表：%s", ", ".join(db.SCHEMA.keys()))


if __name__ == "__main__":
    main()
