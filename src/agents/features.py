"""為各分析師組裝「截至 as_of」的實算特徵（ground truth）。

這些值同時是：
1) 餵給 LLM 分析師的事實輸入（要求它只做解讀、不編造數字）
2) 驗證層比對 LLM 引用數字的基準

嚴格無前視：只用 date <= as_of 的資料。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind
from src.data import query as q


def technical_features(stock_id: str, as_of: str) -> dict:
    """技術面事實：收盤、均線、MACD、RSI、KD、ADX、量能百分位（還原價基礎）。"""
    px = q.get_price(stock_id, end=as_of, adjusted=True)
    if len(px) < 60:
        return {}
    px = ind.add_indicators(px)
    px["adx14"] = ind.adx(px["high"], px["low"], px["close"])
    last = px.iloc[-1]
    vol_pct = float((px["volume"].tail(60).rank(pct=True).iloc[-1]) * 100)
    return {
        "date": last["date"],
        "close": _f(last["close"]),
        "ma5": _f(last["ma5"]), "ma20": _f(last["ma20"]), "ma60": _f(last["ma60"]),
        "macd": _f(last["macd"]), "macd_signal": _f(last["macd_signal"]),
        "rsi14": _f(last["rsi14"]),
        "k": _f(last["k"]), "d": _f(last["d"]),
        "adx14": _f(last["adx14"]),
        "atr14": _f(last["atr14"]),
        "volume": int(last["volume"]) if not pd.isna(last["volume"]) else None,
        "volume_percentile_60d": round(vol_pct, 1),
        "ret_20d": _pct(px["close"], 20),
        "ret_60d": _pct(px["close"], 60),
    }


def chips_features(stock_id: str, as_of: str, lookback: int = 5) -> dict:
    """籌碼面事實：外資/投信近 N 日淨買（股數）。"""
    start = _shift(as_of, lookback * 3 + 10)
    inst = q.get_institutional(stock_id, start, as_of)
    if inst.empty:
        return {"foreign_net_5d": 0, "trust_net_5d": 0}
    dates = sorted(inst["date"].unique())[-lookback:]
    recent = inst[inst["date"].isin(dates)]
    def net(name):
        g = recent[recent["name"] == name]
        return int((g["buy"] - g["sell"]).sum()) if not g.empty else 0
    return {
        "lookback_days": lookback,
        "foreign_net_5d": net("Foreign_Investor"),
        "trust_net_5d": net("Investment_Trust"),
        "dealer_net_5d": net("Dealer_self"),
    }


def fundamental_features(stock_id: str, as_of: str) -> dict:
    """基本面事實：最新月營收與年增率。"""
    rev = q.get_month_revenue(stock_id)
    rev = rev[rev["date"] <= as_of] if not rev.empty else rev
    if rev.empty:
        return {}
    rev = rev.sort_values(["revenue_year", "revenue_month"])
    latest = rev.iloc[-1]
    yr, mo = int(latest["revenue_year"]), int(latest["revenue_month"])
    prev = rev[(rev["revenue_year"] == yr - 1) & (rev["revenue_month"] == mo)]
    yoy = None
    if not prev.empty and prev.iloc[0]["revenue"]:
        yoy = round(float(latest["revenue"]) / float(prev.iloc[0]["revenue"]) - 1.0, 4)
    return {
        "latest_revenue_year": yr,
        "latest_revenue_month": mo,
        "latest_revenue": int(latest["revenue"]) if latest["revenue"] else None,
        "revenue_yoy": yoy,
    }


def _f(v):
    return None if v is None or pd.isna(v) else round(float(v), 2)


def _pct(close: pd.Series, n: int):
    if len(close) <= n or close.iloc[-n - 1] <= 0:
        return None
    return round(float(close.iloc[-1] / close.iloc[-n - 1] - 1.0), 4)


def _shift(date_str: str, days: int) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(date_str) - dt.timedelta(days=days)).isoformat()
