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
    # 下行偏差＝全期 min(excess,0) 的均方根（非「只取負報酬再算 std」——後者會
    # 減去負報酬自身的均值、且分母只除以負報酬筆數，兩處都偏離 Sortino 定義）
    downside = np.minimum(excess, 0.0)
    dd = float(np.sqrt((downside ** 2).mean()))
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

    損益已扣往返交易成本（買賣手續費＋證交稅，比例估算不含最低 20 元）——否則
    貼近損益兩平的策略勝率會被系統性灌水，與含成本的權益曲線口徑不一致。
    Phase 1 為組合層級回測，交易配對僅供參考；精細逐筆歸因於 Phase 5 交易日誌。
    """
    if trades is None or trades.empty:
        return {"win_rate": None, "n_closed": 0}
    from src.env.costs import CostModel
    c = CostModel()
    buy_rate = c.fee_rate * c.fee_discount
    sell_rate = c.fee_rate * c.fee_discount + c.tax_rate
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
                    gross = (sell_px - lot[1]) * matched
                    cost = lot[1] * matched * buy_rate + sell_px * matched * sell_rate
                    realized.append(gross - cost)
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
