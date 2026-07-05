"""決策管線：對單一/多檔股票跑完整 分析師 → 驗證層 → 交易員 流程並存檔。

這是 Phase 2 的核心閉環，供 WebUI「選股報告」頁一鍵觸發。
"""
from __future__ import annotations

import datetime as dt
import json

from src.agents import analysts, trader, validator
from src.config import get_settings
from src.data import database as db
from src.llm import client as llm
from src.logging_setup import get_logger

log = get_logger(__name__)


def analyze_stock(stock_id: str, as_of: str, source: str = "screener") -> dict | None:
    """跑完整決策流程，回傳並存檔一份交易計畫（含各分析師報告與驗證結果）。

    source：候選來源（screener=量化初篩 / news_scout=政策題材偵察），供報告標示。
    """
    llm.new_run()  # 本次管線的所有 LLM 呼叫/攔截共用一個 run_id（大腦活動分組）
    bundle: dict = {}
    tech_feats: dict = {}

    # 1) 技術面
    try:
        rpt, tech_feats = analysts.run_technical(stock_id, as_of)
        if rpt:
            ok, flags, conf = validator.validate_technical(rpt, tech_feats, stock_id, as_of)
            bundle["technical"] = _entry(rpt, conf, flags)
    except Exception as e:  # noqa: BLE001
        log.error("technical %s 失敗：%s", stock_id, e)

    # 2) 籌碼面
    try:
        rpt, feats = analysts.run_chips(stock_id, as_of)
        if rpt:
            ok, flags, conf = validator.validate_chips(rpt, feats, stock_id, as_of)
            bundle["chips"] = _entry(rpt, conf, flags)
    except Exception as e:  # noqa: BLE001
        log.error("chips %s 失敗：%s", stock_id, e)

    # 3) 基本面
    try:
        rpt, feats = analysts.run_fundamental(stock_id, as_of)
        if rpt:
            ok, flags, conf = validator.validate_fundamental(rpt, feats, stock_id, as_of)
            bundle["fundamental"] = _entry(rpt, conf, flags)
    except Exception as e:  # noqa: BLE001
        log.error("fundamental %s 失敗：%s", stock_id, e)

    # 4) 新聞面（按需抓取；冷門股無新聞或額度用罄時自動缺席，不影響其他分析師）
    try:
        rpt, feats = analysts.run_news(stock_id, as_of)
        if rpt:
            ok, flags, conf = validator.validate_news(rpt, feats, stock_id, as_of)
            bundle["news"] = _entry(rpt, conf, flags)
    except Exception as e:  # noqa: BLE001
        log.error("news %s 失敗：%s", stock_id, e)

    if not bundle:
        log.warning("%s 無任何分析師報告（資料不足？）", stock_id)
        return None

    # 5) 交易員綜合決策
    plan = trader.run_trader(stock_id, as_of, bundle, tech_feats)

    record = {
        "as_of": as_of, "stock_id": stock_id, "source": source,
        "plan": plan.model_dump(),
        "analysts": bundle,
    }

    # 6) Guard pipeline：buy 計畫必須通過硬性風控閘門才核准部位（LLM 不可逾越）
    record["guard"] = _run_guard(stock_id, as_of, plan)

    _save_plan(record)
    return record


def _run_guard(stock_id: str, as_of: str, plan) -> dict | None:
    """對 buy 計畫跑 Guard pipeline；駁回寫 friction_log。非 buy 回 None。"""
    if plan.action != "buy":
        return None
    from src.data import fetchers, query as q
    from src.risk import guard as G

    cfg = get_settings()
    rcfg = G.RiskConfig.from_settings(cfg)

    # 產業別（供曝險閘；Phase 5 前組合為空，此欄僅記錄）
    info = q.list_stocks()
    row = info[info["stock_id"] == stock_id]
    industry = row.iloc[0]["industry_category"] if not row.empty else ""

    entry_mid = None
    if plan.entry_low and plan.entry_high:
        entry_mid = (plan.entry_low + plan.entry_high) / 2
    cand = G.TradeCandidate(
        stock_id=stock_id,
        entry=entry_mid or 0.0,
        stop_loss=plan.stop_loss or 0.0,
        target=plan.target_price,
        industry=industry,
    )
    # 組合狀態：用 PaperBroker 的真實持倉/現金/冷卻/回撤（Phase 5）
    try:
        from src.broker.paper import PaperBroker
        port = PaperBroker().portfolio_state(as_of=as_of)
    except Exception as e:  # noqa: BLE001 — 帳本異常時退回空倉保守評估
        log.error("讀取帳本失敗，Guard 以空倉評估：%s", e)
        port = G.PortfolioState.empty(float(cfg["capital"]["total"]))
    with db.connect(cfg.db_path) as conn:
        disp = fetchers.current_disposition_ids(conn, as_of)
        res = G.evaluate(cand, port, rcfg, disposition_ids=disp, as_of=as_of)
        if not res.approved:
            conn.execute(
                "INSERT INTO friction_log (ts, as_of, stock_id, gate, reason, plan_json) "
                "VALUES (?,?,?,?,?,?)",
                (dt.datetime.now().isoformat(timespec="seconds"), as_of, stock_id,
                 res.reject_gate or "", res.reject_reason or "",
                 json.dumps(plan.model_dump(), ensure_ascii=False)),
            )
            log.info("Guard 駁回 %s：[%s] %s", stock_id, res.reject_gate, res.reject_reason)
        else:
            log.info("Guard 核准 %s：%d 股（投入 %s、風險 %s）",
                     stock_id, res.shares, f"{res.est_cost:,.0f}", f"{res.risk_amount:,.0f}")
    return {
        "industry": industry,
        "approved": res.approved, "shares": res.shares,
        "est_cost": res.est_cost, "risk_amount": res.risk_amount,
        "reject_gate": res.reject_gate, "reject_reason": res.reject_reason,
        "checks": res.checks,
    }


def analyze_stocks(stock_ids: list[str], as_of: str,
                   sources: dict[str, str] | None = None) -> list[dict]:
    """對多檔依序分析（供選股報告）。sources: stock_id → 候選來源。"""
    out = []
    for sid in stock_ids:
        rec = analyze_stock(sid, as_of, source=(sources or {}).get(sid, "screener"))
        if rec:
            out.append(rec)
    return out


def _entry(report, confidence, flags) -> dict:
    return {
        "report": report.model_dump(),
        "adjusted_confidence": round(confidence, 3),
        "validation_flags": flags,
    }


def _save_plan(record: dict) -> None:
    p = record["plan"]
    with db.connect(get_settings().db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO trade_plan "
            "(as_of, stock_id, action, action_score, confidence, entry_low, entry_high, "
            " stop_loss, target_price, reward_risk, rationale, plan_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["as_of"], record["stock_id"], p["action"], p["action_score"],
             p["confidence"], p["entry_low"], p["entry_high"], p["stop_loss"],
             p["target_price"], p["reward_risk"], p["rationale"],
             json.dumps(record, ensure_ascii=False),
             dt.datetime.now().isoformat(timespec="seconds")),
        )


def load_plans(as_of: str) -> list[dict]:
    """讀取某日已存的交易計畫（供 WebUI 展示，免重跑）。"""
    with db.connect(get_settings().db_path) as conn:
        rows = db.read_sql(
            conn, "SELECT plan_json FROM trade_plan WHERE as_of=? ORDER BY action_score DESC",
            (as_of,),
        )
    return [json.loads(r) for r in rows["plan_json"]] if not rows.empty else []
