"""績效指標計算：報酬、Sharpe、Sortino、最大回撤、勝率等。"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def total_return(equity: pd.Series) -> float:
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def cagr(equity: pd.Series) -> float:
    n_days = len(equity)
    if n_days < 2:
        return 0.0
    years = n_days / TRADING_DAYS
    if years <= 0 or equity.iloc[0] <= 0:
        return 0.0
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0)


def annual_volatility(daily_ret: pd.Series) -> float:
    return float(daily_ret.std(ddof=0) * np.sqrt(TRADING_DAYS))


def sharpe(daily_ret: pd.Series, rf: float = 0.0) -> float:
    excess = daily_ret - rf / TRADING_DAYS
    sd = excess.std(ddof=0)
    if sd == 0:
        return 0.0
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS))


def sortino(daily_ret: pd.Series, rf: float = 0.0) -> float:
    excess = daily_ret - rf / TRADING_DAYS
    downside = excess[excess < 0]
    dd = downside.std(ddof=0)
    if dd == 0:
        return 0.0
    return float(excess.mean() / dd * np.sqrt(TRADING_DAYS))


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤（負值，如 -0.23 表示 -23%）。"""
    running_max = equity.cummax()
    dd = equity / running_max - 1.0
    return float(dd.min())


def win_rate_from_trades(trades: pd.DataFrame) -> dict:
    """以「配對買賣」粗估每檔平均損益（FIFO）。回傳勝率與盈虧比。

    Phase 1 為組合層級回測，交易配對僅供參考；精細的逐筆損益歸因於 Phase 5 交易日誌處理。
    """
    if trades is None or trades.empty:
        return {"win_rate": None, "n_closed": 0}
    realized = []
    for sid, g in trades.groupby("stock_id"):
        lots = []  # FIFO 佇列 (shares, price)
        for _, t in g.sort_values("date").iterrows():
            if t["side"] == "BUY":
                lots.append([t["shares"], t["price"]])
            else:
                sell_sh, sell_px = t["shares"], t["price"]
                while sell_sh > 0 and lots:
                    lot = lots[0]
                    matched = min(sell_sh, lot[0])
                    realized.append((sell_px - lot[1]) * matched)
                    lot[0] -= matched
                    sell_sh -= matched
                    if lot[0] == 0:
                        lots.pop(0)
    if not realized:
        return {"win_rate": None, "n_closed": 0}
    arr = np.array(realized)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    return {
        "win_rate": float((arr > 0).mean()),
        "n_closed": int(len(arr)),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() != 0 else None,
    }


def summarize(result) -> dict:
    """把 BacktestResult 轉成一組指標 dict（供報告/WebUI）。"""
    eq = result.equity_curve
    ret = result.daily_returns
    m = {
        "strategy": result.strategy_name,
        "initial": round(result.initial_cash, 0),
        "final": round(result.final_equity, 0),
        "total_return": round(total_return(eq), 4),
        "cagr": round(cagr(eq), 4),
        "annual_vol": round(annual_volatility(ret), 4),
        "sharpe": round(sharpe(ret), 3),
        "sortino": round(sortino(ret), 3),
        "max_drawdown": round(max_drawdown(eq), 4),
        "n_days": len(eq),
        "n_trades": 0 if result.trades is None else len(result.trades),
    }
    m.update(win_rate_from_trades(result.trades))
    return m
