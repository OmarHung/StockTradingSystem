"""Guard Pipeline：交易計畫的硬性規則閘門（LLM 不可逾越）。

依序過閘，任一閘不過即駁回並記錄原因（friction log 由呼叫端寫入）：
  ① 黑名單/處置股  ② 回撤熔斷  ③ R:R 下限  ④ 停損冷卻期
  ⑤ 風險部位計算  ⑥ 單股上限  ⑦ 產業曝險上限  ⑧ 現金與持倉數

純函式設計（輸入皆為明確狀態物件），方便單元測試與回測重用。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class TradeCandidate:
    """待審的買進計畫（來自交易員 Agent 或量化策略）。"""
    stock_id: str
    entry: float                 # 進場價（區間中值）
    stop_loss: float
    target: float | None = None
    industry: str = ""


@dataclass
class Position:
    shares: int
    value: float                 # 市值
    industry: str = ""


@dataclass
class PortfolioState:
    """審核當下的組合狀態。Phase 5 前可用 empty() 代表空倉起點。"""
    total_capital: float          # 總資金（權益）
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    # 最近停損日：{stock_id: 'YYYY-MM-DD'}（冷卻期判斷）
    recent_stops: dict[str, str] = field(default_factory=dict)
    peak_equity: float | None = None   # 歷史高點（回撤熔斷）

    @classmethod
    def empty(cls, capital: float) -> "PortfolioState":
        return cls(total_capital=capital, cash=capital, peak_equity=capital)

    @property
    def equity(self) -> float:
        return self.cash + sum(p.value for p in self.positions.values())

    @property
    def drawdown_pct(self) -> float:
        peak = self.peak_equity or self.equity
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - self.equity) / peak * 100)


@dataclass
class RiskConfig:
    per_trade_risk_pct: float = 1.0
    max_single_position_pct: float = 15.0
    min_reward_risk_ratio: float = 1.5
    cooldown_days: int = 5
    max_drawdown_halt_pct: float = 15.0
    max_industry_pct: float = 30.0
    max_positions: int = 10
    blacklist: set[str] = field(default_factory=set)

    @classmethod
    def from_settings(cls, cfg) -> "RiskConfig":
        r = cfg["risk"]
        return cls(
            per_trade_risk_pct=float(r.get("per_trade_risk_pct", 1.0)),
            max_single_position_pct=float(r.get("max_single_position_pct", 15.0)),
            min_reward_risk_ratio=float(r.get("min_reward_risk_ratio", 1.5)),
            cooldown_days=int(r.get("cooldown_days", 5)),
            max_drawdown_halt_pct=float(r.get("max_drawdown_halt_pct", 15.0)),
            max_industry_pct=float(r.get("max_industry_pct", 30.0)),
            max_positions=int(r.get("max_positions", 10)),
        )


@dataclass
class GuardResult:
    approved: bool
    shares: int = 0                       # 核准股數（駁回為 0）
    est_cost: float = 0.0                 # 預估投入金額
    risk_amount: float = 0.0              # 此筆最大虧損金額（到停損）
    reject_gate: str | None = None        # 駁回的閘門名
    reject_reason: str | None = None
    checks: list[dict] = field(default_factory=list)   # 每一閘的紀錄

    def _log(self, gate: str, passed: bool, detail: str) -> None:
        self.checks.append({"gate": gate, "passed": passed, "detail": detail})


def position_size(capital: float, per_trade_risk_pct: float,
                  entry: float, stop_loss: float, lot_size: int = 1000) -> int:
    """風險部位法：股數 = (資金 × 單筆風險%) / 每股停損距離，向下取整到 lot_size。

    lot_size=1000（整張）；資金小到一張都吃不下時退回零股（lot_size=1）。
    """
    risk_budget = capital * per_trade_risk_pct / 100.0
    per_share_risk = entry - stop_loss
    if per_share_risk <= 0:
        return 0
    raw = risk_budget / per_share_risk
    shares = int(raw // lot_size) * lot_size
    if shares == 0 and lot_size > 1:
        shares = int(raw)  # 零股
    return max(shares, 0)


def evaluate(cand: TradeCandidate, port: PortfolioState, cfg: RiskConfig,
             as_of: str | None = None) -> GuardResult:
    """跑完整 Guard pipeline。回傳核准股數或駁回原因（含逐閘紀錄）。"""
    r = GuardResult(approved=False)
    as_of = as_of or dt.date.today().isoformat()

    def reject(gate: str, reason: str) -> GuardResult:
        r._log(gate, False, reason)
        r.reject_gate, r.reject_reason = gate, reason
        return r

    # ① 黑名單（處置股已納入系統，不再攔阻）
    if cand.stock_id in cfg.blacklist:
        return reject("blacklist", f"{cand.stock_id} 在黑名單")
    r._log("blacklist", True, "非黑名單")

    # ② 回撤熔斷
    dd = port.drawdown_pct
    if dd >= cfg.max_drawdown_halt_pct:
        return reject("circuit_breaker",
                      f"組合回撤 {dd:.1f}% ≥ 熔斷門檻 {cfg.max_drawdown_halt_pct}%，停止新倉")
    r._log("circuit_breaker", True, f"回撤 {dd:.1f}% < {cfg.max_drawdown_halt_pct}%")

    # 基本欄位檢查（進出場價需齊備）
    if cand.entry <= 0 or cand.stop_loss <= 0 or cand.stop_loss >= cand.entry:
        return reject("plan_sanity", f"進場/停損不合理（entry={cand.entry}, stop={cand.stop_loss}）")
    r._log("plan_sanity", True, "進場/停損欄位合理")

    # ③ R:R 下限
    if cand.target is not None:
        rr = (cand.target - cand.entry) / (cand.entry - cand.stop_loss)
        if rr < cfg.min_reward_risk_ratio:
            return reject("reward_risk", f"R:R {rr:.2f} < 下限 {cfg.min_reward_risk_ratio}")
        r._log("reward_risk", True, f"R:R {rr:.2f} ≥ {cfg.min_reward_risk_ratio}")
    else:
        return reject("reward_risk", "缺少目標價，無法評估 R:R")

    # ④ 停損冷卻期
    last_stop = port.recent_stops.get(cand.stock_id)
    if last_stop:
        days = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(last_stop)).days
        if days < cfg.cooldown_days:
            return reject("cooldown",
                          f"{cand.stock_id} 於 {last_stop} 停損，冷卻 {cfg.cooldown_days} 天（已過 {days} 天）")
    r._log("cooldown", True, "非冷卻期")

    # ⑤ 風險部位計算
    shares = position_size(port.total_capital, cfg.per_trade_risk_pct,
                           cand.entry, cand.stop_loss)
    if shares <= 0:
        return reject("sizing", "風險預算不足以買進任何股數")
    cost = shares * cand.entry

    # ⑥ 單股上限（超過則縮減部位，而非直接駁回）
    max_pos_value = port.total_capital * cfg.max_single_position_pct / 100.0
    existing = port.positions.get(cand.stock_id)
    existing_value = existing.value if existing else 0.0
    if existing_value + cost > max_pos_value:
        allow_value = max_pos_value - existing_value
        if allow_value <= 0:
            return reject("single_position", f"{cand.stock_id} 已達單股上限 {cfg.max_single_position_pct}%")
        shares = int(allow_value / cand.entry / 1000) * 1000 or int(allow_value / cand.entry)
        if shares <= 0:
            return reject("single_position", "單股上限內已無可加碼空間")
        cost = shares * cand.entry
        r._log("single_position", True, f"縮減至 {shares} 股以符合單股上限")
    else:
        r._log("single_position", True, f"{(existing_value + cost) / port.total_capital * 100:.1f}% ≤ {cfg.max_single_position_pct}%")
    r._log("sizing", True, f"風險部位 {shares} 股（風險 {shares * (cand.entry - cand.stop_loss):,.0f} 元）")

    # ⑦ 產業曝險上限
    if cand.industry:
        industry_value = sum(p.value for p in port.positions.values()
                             if p.industry == cand.industry) + cost
        industry_pct = industry_value / port.total_capital * 100
        if industry_pct > cfg.max_industry_pct:
            return reject("industry_exposure",
                          f"產業「{cand.industry}」曝險 {industry_pct:.1f}% > 上限 {cfg.max_industry_pct}%")
        r._log("industry_exposure", True, f"產業曝險 {industry_pct:.1f}% ≤ {cfg.max_industry_pct}%")

    # ⑧ 現金與持倉數
    if cand.stock_id not in port.positions and len(port.positions) >= cfg.max_positions:
        return reject("max_positions", f"持倉已達上限 {cfg.max_positions} 檔")
    if cost > port.cash:
        # 縮減到現金可負擔（保留 0.5% 手續費緩衝）
        affordable = int(port.cash * 0.995 / cand.entry / 1000) * 1000 or int(port.cash * 0.995 / cand.entry)
        if affordable <= 0:
            return reject("cash", f"現金不足（需 {cost:,.0f}，餘 {port.cash:,.0f}）")
        shares, cost = affordable, affordable * cand.entry
        r._log("cash", True, f"現金受限，縮減至 {shares} 股")
    else:
        r._log("cash", True, "現金充足")

    r.approved = True
    r.shares = shares
    r.est_cost = round(cost, 0)
    r.risk_amount = round(shares * (cand.entry - cand.stop_loss), 0)
    return r
