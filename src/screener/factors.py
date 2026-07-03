"""因子計算：給定 as_of 日期，對股票池每檔算出各因子原始值。

嚴格避免前視偏差：所有計算只使用 date <= as_of 的資料。
回傳一張橫斷面 DataFrame（每列一檔股票，欄位為各因子原始值），
正規化與加權由 screener.py 處理。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.data import query as q


def compute_factors(
    stock_ids: list[str],
    as_of: str,
    momentum_lookback: list[int],
    chips_lookback: int,
    min_avg_turnover: float,
) -> pd.DataFrame:
    """回傳橫斷面因子表。含流動性過濾（不足者剔除）。"""
    # 抓足夠長的價格窗（最長回看 + 緩衝）供動能/均線計算
    max_look = max(momentum_lookback + [60])
    price_start = _shift_days(as_of, max_look * 2 + 20)
    prices = q.get_prices_bulk(stock_ids, price_start, as_of)
    inst = q.get_institutional_bulk(stock_ids, _shift_days(as_of, chips_lookback * 2 + 10), as_of)
    revenue = q.get_revenue_bulk(stock_ids, as_of)

    if prices.empty:
        return pd.DataFrame()

    price_g = {sid: g for sid, g in prices.groupby("stock_id")}
    inst_g = {sid: g for sid, g in inst.groupby("stock_id")} if not inst.empty else {}
    rev_g = {sid: g for sid, g in revenue.groupby("stock_id")} if not revenue.empty else {}

    rows = []
    for sid in stock_ids:
        p = price_g.get(sid)
        if p is None or len(p) < 60:
            continue  # 上市未滿季線者略過
        p = p.sort_values("date")
        close = p["close"].to_numpy()
        last_close = close[-1]

        # 流動性過濾：20 日均成交額
        avg_turnover = p["trading_money"].tail(20).mean()
        if avg_turnover < min_avg_turnover:
            continue

        row = {"stock_id": sid, "close": last_close, "avg_turnover": avg_turnover}

        # 動能：N 日報酬率（過去價為 0/NaN 時視為缺值，避免除零）
        for n in momentum_lookback:
            if len(close) > n and close[-n - 1] > 0 and not np.isnan(close[-n - 1]):
                row[f"momentum_{n}"] = last_close / close[-n - 1] - 1.0
            else:
                row[f"momentum_{n}"] = np.nan

        # 站上季線（ma60）：1/0
        ma60 = p["close"].tail(60).mean()
        row["above_ma60"] = 1.0 if last_close > ma60 else 0.0

        # 籌碼：近 chips_lookback 日 外資+投信 淨買（張，佔比以金額近似）
        row["chips_net_buy"] = _chips_net_buy(inst_g.get(sid), chips_lookback)

        # 月營收年增率（最近一個月 vs 去年同月）
        row["revenue_yoy"] = _revenue_yoy(rev_g.get(sid))

        rows.append(row)

    return pd.DataFrame(rows)


def _chips_net_buy(inst: pd.DataFrame | None, lookback: int) -> float:
    """外資 + 投信 近 lookback 日淨買（買-賣，股數加總）。無資料回 0。"""
    if inst is None or inst.empty:
        return 0.0
    focus = inst[inst["name"].isin(["Foreign_Investor", "Investment_Trust"])].copy()
    if focus.empty:
        return 0.0
    recent_dates = sorted(focus["date"].unique())[-lookback:]
    focus = focus[focus["date"].isin(recent_dates)]
    return float((focus["buy"] - focus["sell"]).sum())


def _revenue_yoy(rev: pd.DataFrame | None) -> float:
    """最新月營收相對去年同月的年增率。資料不足回 NaN。"""
    if rev is None or len(rev) < 13:
        return np.nan
    rev = rev.sort_values(["revenue_year", "revenue_month"])
    latest = rev.iloc[-1]
    yr, mo = int(latest["revenue_year"]), int(latest["revenue_month"])
    prev = rev[(rev["revenue_year"] == yr - 1) & (rev["revenue_month"] == mo)]
    if prev.empty or prev.iloc[0]["revenue"] in (0, None):
        return np.nan
    return float(latest["revenue"]) / float(prev.iloc[0]["revenue"]) - 1.0


def _shift_days(date_str: str, days: int) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(date_str) - dt.timedelta(days=days)).isoformat()
