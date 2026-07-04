"""Guard pipeline 單元測試（純函式，不需 DB/網路）。"""
from __future__ import annotations

from src.risk.guard import (
    Position,
    PortfolioState,
    RiskConfig,
    TradeCandidate,
    evaluate,
    position_size,
)

CFG = RiskConfig(per_trade_risk_pct=1.0, max_single_position_pct=15.0,
                 min_reward_risk_ratio=1.5, cooldown_days=5,
                 max_drawdown_halt_pct=15.0, max_industry_pct=30.0, max_positions=10)


def _cand(**kw):
    base = dict(stock_id="2330", entry=100.0, stop_loss=95.0, target=110.0, industry="半導體")
    base.update(kw)
    return TradeCandidate(**base)


def test_position_size_risk_based():
    # 100 萬 × 1% = 1 萬風險預算；停損距離 5 元 → 2000 股（整張）
    assert position_size(1_000_000, 1.0, 100.0, 95.0) == 2000
    # 停損距離 7 元 → 1428 股 → 整張取 1000
    assert position_size(1_000_000, 1.0, 100.0, 93.0) == 1000
    # 高價股一張吃不下 → 退回零股
    assert position_size(1_000_000, 1.0, 3000.0, 2900.0) == 100
    # 停損高於進場 → 0
    assert position_size(1_000_000, 1.0, 100.0, 105.0) == 0


def test_approve_with_single_position_cap():
    # 風險部位法想買 2000 股（20 萬），但單股上限 15%（15 萬）→ 縮到 1000 股（整張）
    port = PortfolioState.empty(1_000_000)
    r = evaluate(_cand(), port, CFG)
    assert r.approved and r.shares == 1000
    assert r.est_cost == 100000
    assert r.risk_amount == 5000


def test_approve_uncapped():
    # 停損距離 10 元 → 1000 股 = 10 萬 = 10% < 15%，不觸發縮減
    port = PortfolioState.empty(1_000_000)
    r = evaluate(_cand(stop_loss=90.0, target=120.0), port, CFG)
    assert r.approved and r.shares == 1000
    assert r.risk_amount == 10000  # 1000 股 × 10 元 = 1% 資金


def test_reject_low_reward_risk():
    # R:R = (104-100)/(100-95) = 0.8 < 1.5
    r = evaluate(_cand(target=104.0), PortfolioState.empty(1_000_000), CFG)
    assert not r.approved and r.reject_gate == "reward_risk"


def test_reject_cooldown():
    port = PortfolioState.empty(1_000_000)
    port.recent_stops["2330"] = "2026-07-01"
    r = evaluate(_cand(), port, CFG, as_of="2026-07-04")   # 才過 3 天 < 5
    assert not r.approved and r.reject_gate == "cooldown"
    r2 = evaluate(_cand(), port, CFG, as_of="2026-07-08")  # 過 7 天 → 通過
    assert r2.approved


def test_reject_circuit_breaker():
    port = PortfolioState.empty(1_000_000)
    port.peak_equity = 1_000_000
    port.cash = 840_000            # 回撤 16% ≥ 15% 熔斷
    r = evaluate(_cand(), port, CFG)
    assert not r.approved and r.reject_gate == "circuit_breaker"


def test_reject_disposition():
    r = evaluate(_cand(), PortfolioState.empty(1_000_000), CFG,
                 disposition_ids={"2330"})
    assert not r.approved and r.reject_gate == "disposition"


def test_single_position_cap_shrinks():
    # 停損距離極小 → 風險部位法會算出超大股數 → 應被單股 15% 上限縮減
    port = PortfolioState.empty(1_000_000)
    r = evaluate(_cand(entry=100.0, stop_loss=99.5, target=101.0), port, CFG)
    assert r.approved
    assert r.est_cost <= 1_000_000 * 0.15 + 1e-6   # ≤ 15 萬


def test_industry_exposure_reject():
    port = PortfolioState.empty(1_000_000)
    port.cash = 700_000
    port.positions["2454"] = Position(shares=1000, value=290_000, industry="半導體")
    # 新倉 2000 股 × 100 = 20 萬，加上既有 29 萬 → 產業 49% > 30%
    r = evaluate(_cand(), port, CFG)
    assert not r.approved and r.reject_gate == "industry_exposure"


def test_max_positions_reject():
    port = PortfolioState.empty(1_000_000)
    for i in range(10):
        port.positions[f"00{i:02d}"] = Position(shares=100, value=10_000, industry="x")
    r = evaluate(_cand(), port, CFG)
    assert not r.approved and r.reject_gate == "max_positions"


def test_cash_shrink():
    # 名目資金 100 萬（sizing 想買 2000 股=20 萬），但現金只剩 15 萬 → 縮到 1000 股
    port = PortfolioState(total_capital=1_000_000, cash=150_000, peak_equity=None)
    r = evaluate(_cand(), port, CFG)
    assert r.approved and r.shares == 1000  # 縮到現金可負擔（整張）