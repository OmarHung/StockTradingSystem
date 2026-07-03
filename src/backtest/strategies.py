"""回測策略：基準策略 + Screener 策略。

策略只需實作 on_day(date, ctx) -> dict[stock_id, weight] | None。
回傳目標權重（None 表示不調倉）；權重會由回測環境於隔日開盤執行。
所有決策僅能使用 ctx 提供的「截至 date」資料（無前視）。
"""
from __future__ import annotations

import pandas as pd

from src import indicators as ind


class BuyAndHold:
    """買進持有單一標的（基準，預設 0050）。"""

    def __init__(self, stock_id: str = "0050"):
        self.stock_id = stock_id
        self.name = f"BuyAndHold({stock_id})"
        self._entered = False

    def on_day(self, date: str, ctx) -> dict | None:
        if self._entered:
            return None
        # 需有當日資料才進場
        if ctx.close(self.stock_id) is None:
            return None
        self._entered = True
        return {self.stock_id: 1.0}


class MACrossover:
    """單標的均線策略：收盤站上 ma_long 持有，跌破空手（基準）。"""

    def __init__(self, stock_id: str = "0050", ma_long: int = 60):
        self.stock_id = stock_id
        self.ma_long = ma_long
        self.name = f"MACrossover({stock_id},{ma_long})"
        self._in_market = False

    def on_day(self, date: str, ctx) -> dict | None:
        hist = ctx.history(self.stock_id)
        if len(hist) < self.ma_long:
            return None
        ma = hist["close"].tail(self.ma_long).mean()
        price = hist["close"].iloc[-1]
        want = price > ma
        if want and not self._in_market:
            self._in_market = True
            return {self.stock_id: 1.0}
        if not want and self._in_market:
            self._in_market = False
            return {self.stock_id: 0.0}
        return None


class ScreenerStrategy:
    """定期依多因子選股結果等權持有 Top N（Phase 1 量化策略）。

    為避免回測中每日重跑昂貴的全市場選股，改用預先算好的 rebalance 訊號表：
    signals: dict[rebalance_date -> list[stock_id]]。
    """

    def __init__(self, signals: dict[str, list[str]], max_positions: int = 10):
        self.signals = signals
        self.max_positions = max_positions
        self.name = f"Screener(top{max_positions})"
        self._rebalance_days = set(signals.keys())

    def on_day(self, date: str, ctx) -> dict | None:
        if date not in self._rebalance_days:
            return None
        picks = self.signals[date][: self.max_positions]
        if not picks:
            return {}  # 全數出清
        w = 1.0 / len(picks)
        return {sid: w for sid in picks}
