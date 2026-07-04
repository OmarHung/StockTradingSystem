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

_api = None  # 模組級單例（登入一次）


def available() -> bool:
    """套件已安裝且金鑰已設定。"""
    try:
        import shioaji  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("SJ_API_KEY")) and bool(os.getenv("SJ_SEC_KEY"))


def _login():
    global _api
    if _api is not None:
        return _api
    import shioaji as sj

    api = sj.Shioaji(simulation=True)  # 查行情用模擬登入即可
    api.login(os.environ["SJ_API_KEY"], os.environ["SJ_SEC_KEY"])
    _api = api
    log.info("shioaji 已登入（simulation，行情查詢）")
    return _api


# daily_quotes 欄位名對照（shioaji 回傳欄位大小寫歷經版本差異，防禦性對映）
_COL_ALIASES = {
    "code": ["code", "Code"],
    "open": ["open", "Open"],
    "high": ["high", "High"],
    "low": ["low", "Low"],
    "close": ["close", "Close"],
    "volume": ["volume", "Volume", "total_volume", "TotalVolume"],
    "amount": ["amount", "Amount", "total_amount", "TotalAmount"],
}


def _pick(d: dict, keys: list[str]):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def fetch_daily_for_date(conn, date_str: str, wanted_ids: set[str]) -> int:
    """抓某交易日全市場行情，篩 wanted_ids 寫入 price_daily。回傳寫入列數。"""
    api = _login()
    day = dt.date.fromisoformat(date_str)
    quotes = api.daily_quotes(date=day)

    rows = []
    for item in quotes or []:
        d = dict(item) if not isinstance(item, dict) else item
        code = str(_pick(d, _COL_ALIASES["code"]) or "")
        if code not in wanted_ids:
            continue
        close = _pick(d, _COL_ALIASES["close"])
        if close is None:
            continue
        rows.append({
            "stock_id": code, "date": date_str,
            "open": _pick(d, _COL_ALIASES["open"]),
            "high": _pick(d, _COL_ALIASES["high"]),
            "low": _pick(d, _COL_ALIASES["low"]),
            "close": close,
            "volume": _pick(d, _COL_ALIASES["volume"]),
            "trading_money": _pick(d, _COL_ALIASES["amount"]),
        })
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    n = db.upsert_dataframe(conn, "price_daily", df)
    # 更新每檔 fetch_log 已補範圍
    now = dt.datetime.now().isoformat(timespec="seconds")
    for sid in df["stock_id"].unique():
        db.merge_range(conn, "price_daily", sid, date_str, date_str, now)
    return n
