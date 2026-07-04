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


class RiskManagedScreener:
    """Screener 月調倉 + Phase 3 風控：風險部位、ATR 停損、冷卻期、大盤濾網。

    - 部位權重 = min(單筆風險% ÷ 停損距離%, 單股上限%)——風險預算法
    - 每日檢查：收盤跌破停損價 → 出場並記冷卻，冷卻期內調倉不再進同標的
    - 大盤濾網：TAIEX 收盤跌破季線（MA60）→ 全組合曝險 ×0.5（控 MDD 主力）
    - 總權重超過 95% 時等比例縮減（保留現金緩衝）
    """

    MARKET_ID = "TAIEX"

    def __init__(self, signals: dict[str, list[str]], risk_cfg, atr_mult: float = 2.0,
                 max_positions: int = 10, bear_scale: float = 0.5):
        self.signals = signals
        self.rc = risk_cfg
        self.atr_mult = atr_mult
        self.max_positions = max_positions
        self.bear_scale = bear_scale
        self.name = f"RiskScreener(top{max_positions})"
        self._weights: dict[str, float] = {}      # 未縮放的基準權重
        self._stops: dict[str, float] = {}        # sid -> 停損價
        self._stop_dates: dict[str, str] = {}     # sid -> 最近停損日（冷卻）
        self._mkt_scale = 1.0                     # 當前大盤濾網倍率

    def _market_scale(self, ctx) -> float:
        """TAIEX 跌破 MA60 → 降倉倍率；資料不足視為正常。"""
        hist = ctx.history(self.MARKET_ID)
        if len(hist) < 60:
            return 1.0
        ma60 = float(hist["close"].tail(60).mean())
        return self.bear_scale if float(hist["close"].iloc[-1]) < ma60 else 1.0

    def _in_cooldown(self, sid: str, date: str) -> bool:
        import datetime as dt
        last = self._stop_dates.get(sid)
        if not last:
            return False
        return (dt.date.fromisoformat(date) - dt.date.fromisoformat(last)).days < self.rc.cooldown_days

    def on_day(self, date: str, ctx) -> dict | None:
        changed = False

        # 1) 每日停損檢查
        for sid, w in list(self._weights.items()):
            if w <= 0:
                continue
            price = ctx.close(sid)
            stop = self._stops.get(sid)
            if price is not None and stop is not None and price < stop:
                self._weights[sid] = 0.0
                self._stop_dates[sid] = date
                self._stops.pop(sid, None)
                changed = True

        # 2) 調倉日：以風險預算法重建持倉（跳過冷卻期標的）
        if date in self.signals:
            picks = [s for s in self.signals[date] if not self._in_cooldown(s, date)]
            picks = picks[: self.max_positions]
            new_w: dict[str, float] = {}
            new_stops: dict[str, float] = {}
            for sid in picks:
                hist = ctx.history(sid)
                if len(hist) < 20:
                    continue
                entry = float(hist["close"].iloc[-1])
                atr = float(ind.atr(hist["high"], hist["low"], hist["close"]).iloc[-1])
                if not atr or atr != atr or entry <= 0:  # NaN 檢查
                    continue
                stop = entry - self.atr_mult * atr
                if stop <= 0:
                    continue
                stop_dist_pct = (entry - stop) / entry
                w = min((self.rc.per_trade_risk_pct / 100.0) / stop_dist_pct,
                        self.rc.max_single_position_pct / 100.0)
                new_w[sid] = w
                new_stops[sid] = stop
            total = sum(new_w.values())
            if total > 0.95:  # 保留現金緩衝
                scale = 0.95 / total
                new_w = {k: v * scale for k, v in new_w.items()}
            self._weights, self._stops = new_w, new_stops
            changed = True

        # 3) 大盤濾網：倍率變化（多↔空翻轉）即重新縮放整個組合
        scale = self._market_scale(ctx)
        if scale != self._mkt_scale:
            self._mkt_scale = scale
            changed = True

        if not changed:
            return None
        return {sid: w * self._mkt_scale for sid, w in self._weights.items()}


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
