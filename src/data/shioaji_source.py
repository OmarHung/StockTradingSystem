"""shioaji（永豐金）備援資料源：FinMind 額度用罄時接手回補股價日 K。

核心：`api.daily_quotes(date)` 一次回傳「全市場」當日行情——按日呼叫而非按檔，
額度效率極高（補 250 個交易日 = 250 次呼叫，涵蓋所有股票）。

需求：.env 設 SJ_API_KEY / SJ_SEC_KEY（永豐 API 金鑰，simulation 登入即可查行情）。
沒裝套件或沒金鑰時 available() 回 False，呼叫端優雅降級。
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

from src.data import database as db
from src.logging_setup import get_logger

log = get_logger(__name__)

_api = None       # 模組級單例（登入一次）
_api_mode = None  # 登入時的環境；設定改變則重新登入


def current_env() -> str:
    """讀 settings.yaml 的券商環境：simulation（預設）/ production。"""
    from src.config import get_settings
    try:
        get_settings.cache_clear()  # 設定頁可能剛改過
        return (get_settings().get("shioaji") or {}).get("environment", "simulation")
    except Exception:  # noqa: BLE001
        return "simulation"


def available() -> bool:
    """套件已安裝且金鑰已設定。"""
    try:
        import shioaji  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("SJ_API_KEY")) and bool(os.getenv("SJ_SEC_KEY"))


def _login():
    global _api, _api_mode
    mode = current_env()
    if _api is not None and _api_mode == mode:
        return _api
    if _api is not None:  # 環境切換 → 先登出舊連線
        try:
            _api.logout()
        except Exception:  # noqa: BLE001
            pass
        _api = None
    import shioaji as sj

    simulation = mode != "production"
    api = sj.Shioaji(simulation=simulation)
    api.login(os.environ["SJ_API_KEY"], os.environ["SJ_SEC_KEY"])
    _api, _api_mode = api, mode
    log.info("shioaji 已登入（%s）", "simulation 模擬" if simulation else "⚠️ PRODUCTION 正式環境")
    return _api


def fetch_daily_for_date(conn, date_str: str, wanted_ids: set[str]) -> int:
    """抓某交易日全市場行情，篩 wanted_ids 寫入 price_daily。回傳寫入列數。

    DailyQuotes 是「欄向量」結構（column-oriented）：.Code/.Open/.Close 各是
    一條等長陣列，不能按列疊代（d[0] 會炸 'int' object is not 'str'）。
    量能單位已是「股」（實測與 FinMind 完全一致，勿再換算）。
    """
    api = _login()
    day = dt.date.fromisoformat(date_str)
    q = api.daily_quotes(date=day)
    codes = list(getattr(q, "Code", []) or [])
    if not codes:
        return 0

    df = pd.DataFrame({
        "stock_id": [str(c) for c in codes],
        "open": list(q.Open),
        "high": list(q.High),
        "low": list(q.Low),
        "close": list(q.Close),
        "volume": list(q.Volume),
        "trading_money": list(q.Amount),
        "trading_turnover": list(q.Transaction),
    })
    df = df[df["stock_id"].isin(wanted_ids) & (df["close"].astype(float) > 0)].copy()
    if df.empty:
        return 0
    df["date"] = date_str
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")

    n = db.upsert_dataframe(conn, "price_daily", df)
    # 更新每檔 fetch_log 已補範圍
    now = dt.datetime.now().isoformat(timespec="seconds")
    for sid in df["stock_id"].unique():
        db.merge_range(conn, "price_daily", sid, date_str, date_str, now)
    return n
