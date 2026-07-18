"""回測執行器：載入資料、產生選股訊號、跑策略、彙整績效。"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from src.backtest import metrics
from src.backtest.strategies import (
    BuyAndHold,
    MACrossover,
    RiskManagedScreener,
    ScreenerStrategy,
)
from src.config import get_settings
from src.data import query as q
from src.env.costs import CostModel
from src.env.tw_env import Backtester
from src.logging_setup import get_logger
from src.screener.screener import run_screener

log = get_logger(__name__)

# 暖身日曆天：載入 start 之前這麼多天的價格供指標 lookback（MA60/ATR）。
# 60 交易日 ≈ 84 曆日，取 120 留假日緩衝——回測仍只從 start 起跑/計績效，
# 但首個調倉日的 ctx.history 已有足夠歷史，不再首月全空手、MA60 濾網不再前 60 日失效。
_WARMUP_DAYS = 120


def _shift_back(date_str: str, days: int) -> str:
    return (dt.date.fromisoformat(date_str) - dt.timedelta(days=days)).isoformat()


def load_prices(stock_ids: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """把多檔日 K 載入成 {stock_id: DataFrame}（供回測環境）。

    一律用還原價：除權息/分割/減資的跳空不還原會把假跌幅算進報酬
    （0050 一拆四曾讓 buy_and_hold 顯示 -43%）。
    """
    bulk = q.get_prices_bulk(stock_ids, start, end, adjusted=True)
    if bulk.empty:
        return {}
    return {sid: g.reset_index(drop=True) for sid, g in bulk.groupby("stock_id")}


def _month_starts(dates: list[str]) -> list[str]:
    """從交易日清單取每月第一個交易日（作為月調倉日）。"""
    seen, out = set(), []
    for d in sorted(dates):
        ym = d[:7]
        if ym not in seen:
            seen.add(ym)
            out.append(d)
    return out


def generate_screener_signals(
    universe: list[str], rebalance_dates: list[str], cfg=None
) -> dict[str, list[str]]:
    """對每個調倉日跑一次選股，回傳 {date: [stock_id, ...]}。"""
    cfg = cfg or get_settings()
    signals: dict[str, list[str]] = {}
    for d in rebalance_dates:
        ranked = run_screener(d, universe=universe, cfg=cfg)
        signals[d] = [] if ranked.empty else ranked["stock_id"].tolist()
    return signals


def run_backtest(
    strategy_name: str,
    start: str,
    end: str,
    universe: list[str] | None = None,
    initial_cash: float | None = None,
    max_positions: int = 10,
    cfg=None,
):
    """依 strategy_name 執行回測，回傳 (BacktestResult, 指標 dict)。

    strategy_name: 'buy_and_hold' | 'ma_cross' | 'screener'
    """
    cfg = cfg or get_settings()
    initial_cash = initial_cash or cfg["capital"]["total"]
    universe = universe or q.all_stock_ids()
    cost = CostModel()
    warmup_start = _shift_back(start, _WARMUP_DAYS)   # 暖身：多載入 start 之前的歷史

    if strategy_name in ("buy_and_hold", "ma_cross"):
        # 基準以 0050 為主（直接看有無價格資料，不繫於 all_stock_ids——它已排除 ETF）；
        # 無 0050 則退用傳入池的第一檔為基準
        base = "0050"
        prices = load_prices([base], warmup_start, end)
        if not prices:
            base = universe[0]
            prices = load_prices([base], warmup_start, end)
        strat = BuyAndHold(base) if strategy_name == "buy_and_hold" else MACrossover(base)
    elif strategy_name in ("screener", "screener_risk"):
        # TAIEX 一併載入（風控策略的大盤濾網用；不在選股池內）
        prices = load_prices(universe + ["TAIEX"], warmup_start, end)
        # 以月為調倉頻率：月初日只從 [start, end] 窗內取（暖身區間僅供 lookback，
        # 不參與月初判定，否則含 start 的當月月初落在暖身區、首次調倉被推遲一個月）
        all_days = sorted({d for df in prices.values() for d in df["date"]})
        window_days = [d for d in all_days if start <= d <= end]
        rebal = _month_starts(window_days)
        log.info("Screener 回測：%d 個月調倉日", len(rebal))
        signals = generate_screener_signals(universe, rebal, cfg)
        if strategy_name == "screener_risk":
            from src.risk.guard import RiskConfig
            strat = RiskManagedScreener(signals, RiskConfig.from_settings(cfg),
                                        max_positions=max_positions)
        else:
            strat = ScreenerStrategy(signals, max_positions=max_positions)
    else:
        raise ValueError(f"未知策略：{strategy_name}")

    if not prices:
        raise ValueError("回測區間內無價格資料，請先回補資料")

    bt = Backtester(prices, cost_model=cost, initial_cash=initial_cash)
    result = bt.run(strat, start, end)
    return result, metrics.summarize(result)
