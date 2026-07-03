"""設定載入：合併 config/settings.yaml 與 .env 環境變數。

用法：
    from src.config import get_settings
    cfg = get_settings()
    cfg.db_path            # -> "data/market.db"
    cfg["risk"]["cooldown_days"]
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 專案根目錄（本檔案在 src/ 底下）
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = ROOT / "config" / "settings.yaml"


class Settings:
    """薄封裝：既可用屬性存取常用欄位，也可用 dict 方式取巢狀設定。"""

    def __init__(self, raw: dict[str, Any]):
        self._raw = raw

    def __getitem__(self, key: str) -> Any:
        return self._raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    # ---- 常用捷徑 ----
    @property
    def db_path(self) -> Path:
        return ROOT / self._raw["data"]["db_path"]

    @property
    def backfill_start(self) -> str:
        return self._raw["data"]["backfill_start"]

    @property
    def finmind_token(self) -> str | None:
        # token 只從環境變數讀，不寫進 settings.yaml（避免入版控）
        return os.getenv("FINMIND_TOKEN") or None

    @property
    def finmind(self) -> dict[str, Any]:
        return self._raw["data"]["finmind"]

    @property
    def log_dir(self) -> Path:
        return ROOT / self._raw["logging"]["dir"]

    @property
    def log_level(self) -> str:
        return self._raw["logging"]["level"]


@lru_cache(maxsize=1)
def get_settings(path: str | Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """載入設定（結果快取；測試可傳入不同 path 但需先 get_settings.cache_clear()）。"""
    load_dotenv(ROOT / ".env")  # 若無 .env 亦不報錯
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings(raw)
