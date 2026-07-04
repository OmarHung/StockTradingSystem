"""反思引擎：定期回顧「決策 vs 實際結果」，用 LLM 合成規則與反模式。

流程（CryptoTrade reflection + LLM_trader 規則合成）：
  1. 收集已評估經驗的統計（勝率、各 outcome 分布、贏家/輸家情境樣本）
  2. 收集 friction（被擋交易）分布
  3. 反思 LLM 歸納 →「有效規則」「反模式」＋風格建議（保守↔積極）
  4. 規則存入 rules collection → 之後每次交易員決策時語意檢索注入
"""
from __future__ import annotations

import datetime as dt
import json

from pydantic import BaseModel, Field

from src.config import get_settings
from src.data import database as db
from src.llm import client as llm
from src.logging_setup import get_logger
from src.memory import store

log = get_logger(__name__)


class RuleItem(BaseModel):
    text: str = Field(description="一條可操作的規則（繁體中文，具體、可檢驗）")
    kind: str = Field(description="effective=有效規則 / anti_pattern=反模式（該避免的）")
    evidence: str = Field(description="支持此規則的證據摘要（引用實際勝率/案例）")


class ReflectionOutput(BaseModel):
    summary: str = Field(description="本期檢討總結（繁體中文，3-5 句）")
    style_advice: str = Field(description="風格建議：conservative / neutral / aggressive，附一句理由")
    rules: list[RuleItem] = Field(description="歸納出的規則（最多 5 條，寧缺勿濫）")


def _gather_stats() -> dict:
    """整理反思素材：outcome 統計 + 贏家/輸家樣本 + friction 分布。"""
    with db.connect(get_settings().db_path) as conn:
        plans = db.read_sql(
            conn, "SELECT stock_id, as_of, action, outcome, outcome_return, plan_json "
                  "FROM trade_plan WHERE evaluated_at IS NOT NULL")
        friction = db.read_sql(
            conn, "SELECT gate, COUNT(*) AS n FROM friction_log GROUP BY gate")
    if plans.empty:
        return {}

    buys = plans[plans["action"] == "buy"]
    stats: dict = {
        "total_evaluated": len(plans),
        "outcome_counts": plans["outcome"].value_counts().to_dict(),
        "buy_count": len(buys),
    }
    if not buys.empty:
        rets = buys["outcome_return"].dropna()
        stats["buy_win_rate"] = round(float((rets > 0).mean()), 3) if len(rets) else None
        stats["buy_avg_return"] = round(float(rets.mean()), 4) if len(rets) else None

    # 贏家/輸家情境樣本（給 LLM 歸納模式）
    def _samples(df, n):
        out = []
        for r in df.head(n).to_dict(orient="records"):
            rec = json.loads(r["plan_json"])
            from src.memory.outcome import _situation_text
            out.append({"situation": _situation_text(rec),
                        "outcome": r["outcome"], "ret": r["outcome_return"]})
        return out

    evaluated_buys = buys.dropna(subset=["outcome_return"])
    winners = evaluated_buys.sort_values("outcome_return", ascending=False)
    losers = evaluated_buys.sort_values("outcome_return")
    stats["winner_samples"] = _samples(winners, 5)
    stats["loser_samples"] = _samples(losers, 5)
    stats["friction_by_gate"] = friction.set_index("gate")["n"].to_dict() if not friction.empty else {}
    return stats


def run_reflection() -> dict | None:
    """執行一次深度反思。回傳結果 dict（含寫入的規則數）；素材不足回 None。"""
    stats = _gather_stats()
    if not stats or stats.get("total_evaluated", 0) < 3:
        log.info("已評估決策不足 3 筆，暫不反思（先累積經驗）")
        return None

    cfg = get_settings()
    system = (
        "你是交易系統的反思分析師。根據「過去決策 vs 實際結果」的統計與樣本，"
        "歸納可操作的規則與反模式。要求：只根據提供的證據歸納、規則要具體可檢驗、"
        "寧缺勿濫（沒有明確模式就少寫）。輸出繁體中文。"
    )
    prompt = (
        f"以下是截至目前的決策成果統計與樣本：\n"
        f"{json.dumps(stats, ensure_ascii=False, indent=2)}\n\n"
        "請歸納：1) 哪些情境特徵與好結果相關（有效規則）"
        "2) 哪些與壞結果相關（反模式）3) 目前該偏保守還是積極。"
    )
    out: ReflectionOutput = llm.call_structured(
        model=cfg["llm"]["reflection_model"], system=system, user_prompt=prompt,
        schema=ReflectionOutput, agent="reflection", max_tokens=4000,
    )

    now = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    for i, rule in enumerate(out.rules):
        kind = rule.kind if rule.kind in ("effective", "anti_pattern") else "effective"
        store.add_rule(f"rule_{now}_{i}", rule.text, kind, rule.evidence)
    log.info("反思完成：%d 條規則入庫｜風格建議 %s", len(out.rules), out.style_advice)
    return {"summary": out.summary, "style_advice": out.style_advice,
            "rules_added": len(out.rules)}


def memory_context(situation: str) -> str:
    """組出注入交易員 prompt 的「歷史經驗與規則」段落（無資料回空字串）。"""
    exps = store.query_experiences(situation, n=3)
    rules = store.query_rules(situation, n=5)
    if not exps and not rules:
        return ""
    lines = []
    if rules:
        lines.append("【過往反思歸納的規則（請納入考量）】")
        for r in rules:
            tag = "✅有效" if r["kind"] == "effective" else "⚠️反模式"
            lines.append(f"- [{tag}] {r['text']}")
    if exps:
        lines.append("【語意最相似的歷史決策與結果】")
        for e in exps:
            m = e["meta"]
            lines.append(f"- {e['text'][:100]} → 結果 {m.get('outcome')}（報酬 {m.get('ret')}）")
    return "\n".join(lines)