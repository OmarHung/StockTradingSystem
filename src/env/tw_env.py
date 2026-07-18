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

    def ohlc(self, stock_id: str, date: str | None = None) -> dict | None:
        """該股票某日的 open/high/low/close（供停損盤中觸價判定）。無資料回 None。"""
        df = self._prices.get(stock_id)
        if df is None:
            return None
        d = date or self._cur
        if d not in df.index:
            return None
        row = df.loc[d]
        out = {}
        for k in ("open", "high", "low", "close"):
            v = row[k] if k in row else None
            out[k] = float(v) if v is not None and not pd.isna(v) else None
        return out


@dataclass
class BacktestResult:
    strategy_name: str
    equity_curve: pd.Series               # index=date, value=總權益
    trades: pd.DataFrame                  # 逐筆成交紀錄
    daily_returns: pd.Series
    initial_cash: float
    final_equity: float
    meta: dict = field(default_factory=dict)


# 持股連續無資料超過此天數視為疑似長期停牌/下市，估值改用保守折價（見 _holdings_value）。
# 20 個交易日 ≈ 一個月，區別於除權息前後的短暫缺漏。
_STALE_HALT_DAYS = 20
# 疑似停牌/下市股的保守估值折價（停牌多伴隨重大利空，避免以停牌前價高估權益）。
_STALE_HAIRCUT = 0.5


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
        self._stale_flag = False   # 回測期間曾對疑似停牌/下市股套用折價估值

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
        # 首日 pct_change 為 NaN，直接 dropna 剔除（非 fillna(0)）——那個假 0 會同時
        # 壓低平均報酬、改變標準差，稀釋 Sharpe/Sortino/年化波動（短窗回測偏差最大）
        daily_ret = equity_curve.pct_change().dropna()
        trades_df = pd.DataFrame(trades)

        return BacktestResult(
            strategy_name=getattr(strategy, "name", strategy.__class__.__name__),
            equity_curve=equity_curve,
            trades=trades_df,
            daily_returns=daily_ret,
            initial_cash=self.initial_cash,
            final_equity=float(equity_curve.iloc[-1]),
            meta={"start": start, "end": end, "n_days": len(days),
                  "stale_valuation": self._stale_flag},
        )

    # ---- 內部：調倉執行 ----
    def _open_price(self, date: str, sid: str) -> float | None:
        df = self.prices.get(sid)
        if df is not None and date in df.index:
            val = df.at[date, "open"]
            return float(val) if not pd.isna(val) else None
        return None

    def _last_close(self, date: str, sid: str) -> float | None:
        """date（含）之前最近一筆收盤價；該股完全無資料回 None。"""
        df = self.prices.get(sid)
        if df is None or df.empty:
            return None
        i = df.index.searchsorted(date, side="right") - 1
        if i < 0:
            return None
        v = df["close"].iloc[i]
        return float(v) if not pd.isna(v) else None

    def _stale_days(self, date: str, sid: str) -> int:
        """該股最近一筆資料日距 date 的曆日差；無資料回 -1、當日有資料回 0。"""
        df = self.prices.get(sid)
        if df is None or df.empty:
            return -1
        i = df.index.searchsorted(date, side="right") - 1
        if i < 0:
            return -1
        last = df.index[i]
        if last >= date:
            return 0
        import datetime as _dt
        try:
            return (_dt.date.fromisoformat(date) - _dt.date.fromisoformat(str(last))).days
        except ValueError:
            return 0

    def _holdings_value(self, date: str, positions: dict[str, int]) -> float:
        """持股市值。當日停牌/資料未入庫者以最近收盤價估值——
        估 0 會讓權益曲線出現假暴跌（隔日又彈回），波動率與 MDD 全失真。

        但長期停牌（處置/下市前停牌）以停牌前價一路結轉會人為平滑波動、低估 MDD，
        極端下市股甚至永遠計入停牌前市值而虛增權益；故超過 _STALE_HALT_DAYS 未見資料者
        改以保守折價估值，並記旗標於 meta 警示。"""
        total = 0.0
        for sid, sh in positions.items():
            px = self._last_close(date, sid)
            if px is None:
                continue
            stale = self._stale_days(date, sid)
            if stale > _STALE_HALT_DAYS:
                px *= _STALE_HAIRCUT
                self._stale_flag = True
            total += sh * px
        return total

    def _rebalance(self, date, cash, positions, targets):
        """依目標權重於 date 開盤價調整持股。回傳 (cash, positions, trades)。"""
        # 以「調倉前」總權益（用當日開盤價估）為基準分配
        open_prices = {sid: self._open_price(date, sid) for sid in set(list(positions) + list(targets))}
        equity = cash + sum(
            sh * (open_prices[sid] if open_prices.get(sid) is not None
                  else (self._last_close(date, sid) or 0.0))
            for sid, sh in positions.items()
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
                # 用有效費率粗估後，以「真正的 buy_cost（含 min_fee 與 fee_discount）」逐手
                # 回退複核——粗估式忽略 min_fee 底價與折數，小額/低價股會把現金買成負值
                eff_rate = self.cost.fee_rate * self.cost.fee_discount
                affordable = int(cash / (px * (1 + eff_rate)) // self.lot_size) * self.lot_size
                buy_sh = max(affordable, 0)
                while buy_sh > 0 and buy_sh * px + self.cost.buy_cost(buy_sh * px) > cash:
                    buy_sh -= self.lot_size
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
