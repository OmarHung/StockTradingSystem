"""交易員 Agent：綜合各分析師（經驗證層調整信心後）報告 + 持倉狀態，

輸出 action score 與完整進出場計畫（進場區間/停損/目標價/R:R）。
用 Opus 4.8 + adaptive thinking。
"""
from __future__ import annotations

import json

from src.config import get_settings
from src.llm import client as llm
from src.llm.schemas import TradePlan, normalize_scores


def run_trader(stock_id: str, as_of: str, analyst_bundle: dict, tech_feats: dict) -> TradePlan:
    """analyst_bundle: {agent: {"report": {...}, "confidence": float, "flags": [...]}}"""
    model = get_settings()["llm"]["trader_model"]
    close = tech_feats.get("close")
    atr = tech_feats.get("atr14")

    system = (
        "你是嚴謹的台股交易員，綜合技術/籌碼/基本面分析師的報告做最終決策。"
        "分析師報告已附『經驗證層調整後的信心』與『驗證攔截標記』——"
        "對被攔截（flags 非空）的報告要保守看待。輸出繁體中文。"
        "進出場計畫要具體：進場價區間、停損價、目標價，並確保報酬風險比 reward_risk = "
        "(目標-進場中值)/(進場中值-停損) 至少 1.5 才建議 buy，否則 hold 或 avoid。"
        f"參考：目前收盤約 {close}，ATR14 約 {atr}（可用於設定停損距離）。"
    )
    prompt = (
        f"股票代號 {stock_id}，基準日 {as_of}。\n各分析師報告與驗證結果：\n"
        f"{json.dumps(analyst_bundle, ensure_ascii=False, indent=2)}\n\n"
        "請綜合判斷，輸出 action(buy/hold/avoid)、action_score[-1,1]、confidence、"
        "進出場計畫與理由。目前無持倉。"
    )
    plan = llm.call_structured(
        model=model, system=system, user_prompt=prompt, schema=TradePlan,
        agent="trader", stock_id=stock_id, as_of=as_of, max_tokens=6000, use_thinking=True,
    )
    return normalize_scores(plan)
