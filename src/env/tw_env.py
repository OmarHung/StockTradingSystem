"""TWEnv — 台股事件驅動日線組合回測環境。

設計原則：
- **無前視偏差**：策略在第 d 日收盤後決策（可用 <= d 的所有資料），
  委託於第 d+1 日「開盤價」成交。
- **成本內化**：買賣皆透過 CostModel 計入手續費/證交稅。
- **零股交易**：以整股（1 股）為最小單位（台股自 2020 起有盤中零股），
  使 Top-N 小資金組合可行；lot_size 可調。

策略介面（見 backtest/strategies.py）：
    class Strategy:
        name: str
        def on_day(self, date: str, ctx: Context) -> dict[str, float] | None
            # 回傳目標權重 {stock_id: weight}；None 表示當日不調倉
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.env.costs import CostModel
from src.logging_setup import get_logger

log = get_logger(__name__)


class Context:
    """提供策略「截至某日（含）」的無前視資料存取。"""

    def __init__(self, prices: dict[str, pd.DataFrame]):
        self._prices = prices
        self._cur: str = ""

    def _set_date(self, date: str) -> None:
        self._cur = date

    def history(self, stock_id: str) -> pd.DataFrame:
        """該股票截至目前日期（含）的歷史日 K。"""
        df = self._prices.get(stock_id)
        if df is None:
            return pd.DataFrame()
        return df[df.index <= self._cur]

    def close(self, stock_id: str, date: str | None = None) -> float | None:
        df = self._prices.get(stock_id)
        if df is None:
            return None
        d = date or self._cur
        if d in df.index:
            return float(df.at[d, "close"])
        return None


@dataclass
class BacktestResult:
    strategy_name: str
    equity_curve: pd.Series               # index=date, value=總權益
    trades: pd.DataFrame                  # 逐筆成交紀錄
    daily_returns: pd.Series
    initial_cash: float
    final_equity: float
    meta: dict = field(default_factory=dict)


class Backtester:
    def __init__(
        self,
        prices: dict[str, pd.DataFrame],
        cost_model: CostModel | None = None,
        initial_cash: float = 1_000_000,
        lot_size: int = 1,
    ):
        # prices: stock_id -> DataFrame(index=date str, 欄位含 open/high/low/close/volume)
        self.prices = {sid: df.set_index("date").sort_index() for sid, df in prices.items()}
        self.cost = cost_model or CostModel()
        self.initial_cash = initial_cash
        self.lot_size = lot_size

    def _trading_days(self, start: str, end: str) -> list[str]:
        days: set[str] = set()
        for df in self.prices.values():
            days.update(df.index[(df.index >= start) & (df.index <= end)])
        return sorted(days)

    def run(self, strategy, start: str, end: str) -> BacktestResult:
        ctx = Context(self.prices)
        days = self._trading_days(start, end)
        if not days:
            raise ValueError("回測區間內無交易日資料")

        cash = self.initial_cash
        positions: dict[str, int] = {}
        pending: dict[str, float] | None = None   # 待第 d+1 日開盤執行的目標權重
        equity_rows, trades = [], []

        for i, d in enumerate(days):
            # 1) 若有前一日決策，於今日開盤執行調倉
            if pending is not None:
                cash, positions, filled = self._rebalance(d, cash, positions, pending)
                trades.extend(filled)
                pending = None

            # 2) 以今日收盤計算權益（mark-to-market）
            equity = cash + self._holdings_value(d, positions)
            equity_rows.append((d, equity))

            # 3) 策略於今日收盤後決策（可用 <= d 全部資料），明日開盤執行
            ctx._set_date(d)
            targets = strategy.on_day(d, ctx)
            if targets is not None and i < len(days) - 1:
                pending = targets

        equity_curve = pd.Series(
            [e for _, e in equity_rows], index=[d for d, _ in equity_rows], name="equity"
        )
        daily_ret = equity_curve.pct_change().fillna(0.0)
        trades_df = pd.DataFrame(trades)

        return BacktestResult(
            strategy_name=getattr(strategy, "name", strategy.__class__.__name__),
            equity_curve=equity_curve,
            trades=trades_df,
            daily_returns=daily_ret,
            initial_cash=self.initial_cash,
            final_equity=float(equity_curve.iloc[-1]),
            meta={"start": start, "end": end, "n_days": len(days)},
        )

    # ---- 內部：調倉執行 ----
    def _open_price(self, date: str, sid: str) -> float | None:
        df = self.prices.get(sid)
        if df is not None and date in df.index:
            val = df.at[date, "open"]
            return float(val) if not pd.isna(val) else None
        return None

    def _holdings_value(self, date: str, positions: dict[str, int]) -> float:
        total = 0.0
        for sid, sh in positions.items():
            df = self.prices.get(sid)
            if df is not None and date in df.index:
                total += sh * float(df.at[date, "close"])
        return total

    def _rebalance(self, date, cash, positions, targets):
        """依目標權重於 date 開盤價調整持股。回傳 (cash, positions, trades)。"""
        # 以「調倉前」總權益（用當日開盤價估）為基準分配
        open_prices = {sid: self._open_price(date, sid) for sid in set(list(positions) + list(targets))}
        equity = cash + sum(
            sh * open_prices[sid] for sid, sh in positions.items()
            if open_prices.get(sid) is not None
        )
        filled = []

        # 目標張數
        desired: dict[str, int] = {}
        for sid, w in targets.items():
            px = open_prices.get(sid)
            if px is None or px <= 0:
                continue
            target_value = max(w, 0.0) * equity
            shares = int(target_value // (px * self.lot_size)) * self.lot_size
            desired[sid] = shares

        # 先賣（釋放現金）再買
        for sid, cur in list(positions.items()):
            tgt = desired.get(sid, 0)
            px = open_prices.get(sid)
            if px is None:
                continue
            if tgt < cur:
                sell_sh = cur - tgt
                amount = sell_sh * px
                cash += amount - self.cost.sell_cost(amount)
                positions[sid] = tgt
                filled.append(_trade(date, sid, "SELL", sell_sh, px, amount))
                if tgt == 0:
                    positions.pop(sid, None)

        for sid, tgt in desired.items():
            cur = positions.get(sid, 0)
            px = open_prices.get(sid)
            if px is None or tgt <= cur:
                continue
            buy_sh = tgt - cur
            amount = buy_sh * px
            cost = self.cost.buy_cost(amount)
            if amount + cost > cash:  # 現金不足則買可負擔的最大張數
                affordable = int(cash / (px * (1 + self.cost.fee_rate)) // self.lot_size) * self.lot_size
                buy_sh = max(affordable - 0, 0)
                if buy_sh <= 0:
                    continue
                amount = buy_sh * px
                cost = self.cost.buy_cost(amount)
            cash -= amount + cost
            positions[sid] = cur + buy_sh
            filled.append(_trade(date, sid, "BUY", buy_sh, px, amount))

        return cash, positions, filled


def _trade(date, sid, side, shares, price, amount) -> dict:
    return {"date": date, "stock_id": sid, "side": side,
            "shares": int(shares), "price": round(price, 2), "amount": round(amount, 0)}
