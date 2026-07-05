"""實倉（模擬）績效統計：從 equity_history + fills 計算完整指標。

與回測共用 metrics 模組的數學，確保口徑一致。
"""
from __future__ import annotations

import pandas as pd

from src.backtest import metrics
from src.broker.paper import PaperBroker


def performance_summary() -> dict:
    """回傳績效摘要 + 權益曲線 + 已實現交易統計（供 API/UI）。"""
    broker = PaperBroker()
    eq = broker.equity_history()
    fills = broker.fills(limit=10000)

    out: dict = {"has_data": not eq.empty}
    if eq.empty:
        return out

    equity = pd.Series(eq["equity"].values, index=eq["date"].values)
    daily_ret = equity.pct_change().fillna(0.0)

    out.update({
        "start_date": eq["date"].iloc[0],
        "end_date": eq["date"].iloc[-1],
        "days": len(eq),
        "initial": float(equity.iloc[0]),
        "current": float(equity.iloc[-1]),
        "total_return": round(metrics.total_return(equity), 4) if len(equity) > 1 else 0.0,
        "sharpe": round(metrics.sharpe(daily_ret), 3),
        "sortino": round(metrics.sortino(daily_ret), 3),
        "max_drawdown": round(metrics.max_drawdown(equity), 4),
        "annual_vol": round(metrics.annual_volatility(daily_ret), 4),
    })

    # vs TAIEX（同期間）
    taiex = eq.dropna(subset=["taiex_close"])
    if len(taiex) > 1:
        t0, t1 = float(taiex["taiex_close"].iloc[0]), float(taiex["taiex_close"].iloc[-1])
        out["taiex_return"] = round(t1 / t0 - 1, 4)
        out["alpha"] = round(out["total_return"] - out["taiex_return"], 4)

    # 已實現交易統計（賣出 fills）
    sells = fills[fills["side"] == "SELL"].dropna(subset=["pnl"]) if not fills.empty else pd.DataFrame()
    if not sells.empty:
        wins = sells[sells["pnl"] > 0]
        losses = sells[sells["pnl"] < 0]
        out["closed_trades"] = len(sells)
        out["win_rate"] = round(len(wins) / len(sells), 3)
        out["total_realized_pnl"] = round(float(sells["pnl"].sum()), 0)
        out["profit_factor"] = (
            round(float(wins["pnl"].sum() / -losses["pnl"].sum()), 2)
            if not losses.empty and losses["pnl"].sum() != 0 else None)
    else:
        out["closed_trades"] = 0

    out["equity_curve"] = [
        {"time": r.date, "value": float(r.equity)} for r in eq.itertuples()]
    # 大盤正規化到同起點（畫對照線）
    if len(taiex) > 1:
        base = float(equity.iloc[0])
        out["taiex_curve"] = [
            {"time": r.date, "value": round(base * float(r.taiex_close) / t0, 0)}
            for r in taiex.itertuples()]
    return _sanitize(out)


def _sanitize(obj):
    """NaN/Inf → None（JSON 不合法；如 sortino 在無負報酬日時下檔標準差=0 → NaN）。"""
    import math
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj