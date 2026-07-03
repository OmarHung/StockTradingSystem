"""回測引擎、成本模型、指標的離線測試（合成資料，不需連網）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest import metrics
from src.env.costs import CostModel
from src.env.tw_env import Backtester


def _synthetic_prices(sid: str, dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "stock_id": sid, "date": dates,
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1_000_000] * len(dates),
    })


def test_cost_model():
    c = CostModel()
    # 買 100 萬：手續費 1425 元
    assert abs(c.buy_cost(1_000_000) - 1425) < 1e-6
    # 賣 100 萬：手續費 1425 + 證交稅 3000 = 4425
    assert abs(c.sell_cost(1_000_000) - 4425) < 1e-6
    # 最低手續費 20 元
    assert c.buy_cost(1000) == 20


def test_no_lookahead_execution():
    """策略在第 d 日決策，應於第 d+1 日開盤成交，而非當日。"""
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    prices = {"A": _synthetic_prices("A", dates, [100.0, 110.0, 120.0])}

    class BuyDay0:
        name = "buyday0"
        def __init__(self): self.done = False
        def on_day(self, date, ctx):
            if not self.done:
                self.done = True
                return {"A": 1.0}
            return None

    bt = Backtester(prices, CostModel(), initial_cash=100_000, lot_size=1)
    res = bt.run(BuyDay0(), "2024-01-02", "2024-01-04")
    # 第一天(1/02)決策 → 第二天(1/03)開盤價 110 成交，非當日 100
    buy = res.trades.iloc[0]
    assert buy["date"] == "2024-01-03"
    assert buy["price"] == 110.0


def test_metrics_max_drawdown():
    eq = pd.Series([100, 120, 90, 110], index=["a", "b", "c", "d"])
    # 峰值 120 → 谷 90，回撤 = 90/120 - 1 = -0.25
    assert abs(metrics.max_drawdown(eq) - (-0.25)) < 1e-9


def test_metrics_total_return():
    eq = pd.Series([1000, 1100], index=["a", "b"])
    assert abs(metrics.total_return(eq) - 0.1) < 1e-9


def test_sharpe_zero_vol():
    # 零波動不應除零爆炸
    assert metrics.sharpe(pd.Series([0.0, 0.0, 0.0])) == 0.0
