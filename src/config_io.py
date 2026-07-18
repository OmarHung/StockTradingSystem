"""settings.yaml 的讀寫（供 WebUI 設定頁表單化編輯）。

與 config.py（唯讀、快取）分工：此模組負責「寫回」並清快取，讓設定變更即時生效。
使用 ruamel.yaml 做往返，**保留註解與排版**——即使透過 WebUI 存檔，設定檔的說明也不會遺失。
寫入採「先寫暫存檔再替換」避免中途損毀。
"""
from __future__ import annotations

import os
from pathlib import Path

from ruamel.yaml import YAML

from src.config import DEFAULT_SETTINGS_PATH, ROOT, get_settings

ENV_PATH = ROOT / ".env"

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def load_raw(path: str | Path = DEFAULT_SETTINGS_PATH):
    """回傳 ruamel 的 CommentedMap（可當一般 dict 用，但保留註解）。"""
    with open(path, "r", encoding="utf-8") as f:
        return _yaml.load(f)


def save_raw(data, path: str | Path = DEFAULT_SETTINGS_PATH) -> None:
    path = Path(path)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    os.replace(tmp, path)
    get_settings.cache_clear()  # 讓下次 get_settings() 讀到新值


def update_section(section: str, values: dict, path: str | Path = DEFAULT_SETTINGS_PATH) -> None:
    """更新某個頂層區塊（淺層合併）並存檔，保留原有註解。"""
    data = load_raw(path)
    if section not in data or data[section] is None:
        data[section] = {}
    for k, v in values.items():
        data[section][k] = v
    save_raw(data, path)


def set_env_var(key: str, value: str, path: str | Path = ENV_PATH) -> None:
    """寫入/更新 .env 的一個變數（供 WebUI 設定 API token）。同時更新當前進程環境。

    key/value 拒絕換行與控制字元：含 '\\n' 的 value 會被寫成 .env 的多行，等於能
    注入任意環境變數（如改寫 ANTHROPIC_BASE_URL 竊金鑰）。
    """
    if any(ord(c) < 0x20 or c in "\r\n" for c in f"{key}{value}"):
        raise ValueError("環境變數的名稱/值不可含換行或控制字元")
    if "=" in key or not key.strip():
        raise ValueError("環境變數名稱不合法")
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith(f"{key}=") or ln.strip().startswith(f"{key} ="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value  # 立即生效（同進程）
    get_settings.cache_clear()
