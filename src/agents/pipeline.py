"""決策管線：對單一/多檔股票跑完整 分析師 → 驗證層 → 交易員 流程並存檔。

這是 Phase 2 的核心閉環，供 WebUI「選股報告」頁一鍵觸發。
"""
from __future__ import annotations

import datetime as dt
import json

from src.agents import analysts, trader, validator
from src.config import get_settings
from src.data import database as db
from src.logging_setup import get_logger

log = get_logger(__name__)


def analyze_stock(stock_id: str, as_of: str) -> dict | None:
    """跑完整決策流程，回傳並存檔一份交易計畫（含各分析師報告與驗證結果）。"""
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

    if not bundle:
        log.warning("%s 無任何分析師報告（資料不足？）", stock_id)
        return None

    # 4) 交易員綜合決策
    plan = trader.run_trader(stock_id, as_of, bundle, tech_feats)

    record = {
        "as_of": as_of, "stock_id": stock_id,
        "plan": plan.model_dump(),
        "analysts": bundle,
    }
    _save_plan(record)
    return record


def analyze_stocks(stock_ids: list[str], as_of: str) -> list[dict]:
    """對多檔依序分析（供選股報告）。回傳成功的紀錄清單。"""
    out = []
    for sid in stock_ids:
        rec = analyze_stock(sid, as_of)
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
