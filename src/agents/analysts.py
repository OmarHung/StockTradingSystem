"""三位資料型分析師：技術面、籌碼面、基本面。

每位分析師拿到「實算特徵（ground truth）」，只做專業解讀並輸出結構化報告，
並在 cited_* 欄位回填它引用的關鍵數字，供驗證層交叉比對。
"""
from __future__ import annotations

import json

from src.agents import features as F
from src.config import get_settings
from src.llm import client as llm
from src.llm.schemas import (
    ChipsReport,
    FundamentalReport,
    TechnicalReport,
    normalize_scores,
)

_ANALYST_MODEL = None


def _model() -> str:
    return get_settings()["llm"]["analyst_model"]


def run_technical(stock_id: str, as_of: str) -> tuple[TechnicalReport | None, dict]:
    feats = F.technical_features(stock_id, as_of)
    if not feats:
        return None, feats
    system = (
        "你是專業台股技術分析師。根據提供的『實算技術指標』做客觀解讀，"
        "只能引用提供的數字，不得自行編造。輸出繁體中文。"
        "若引用 ADX 或 RSI，請在 cited_adx / cited_rsi 填入你引用的『實算值』。"
    )
    prompt = (
        f"股票代號 {stock_id}，基準日 {as_of}。實算技術指標如下：\n"
        f"{json.dumps(feats, ensure_ascii=False, indent=2)}\n\n"
        "請判斷技術面方向（bullish/neutral/bearish）、給出 score[-1,1] 與 confidence[0,1]，"
        "並列出關鍵觀察與總結。"
    )
    rpt = llm.call_structured(
        model=_model(), system=system, user_prompt=prompt, schema=TechnicalReport,
        agent="technical", stock_id=stock_id, as_of=as_of,
    )
    return normalize_scores(rpt) if rpt else rpt, feats


def run_chips(stock_id: str, as_of: str) -> tuple[ChipsReport | None, dict]:
    feats = F.chips_features(stock_id, as_of)
    system = (
        "你是專業台股籌碼分析師，專精三大法人動向。根據提供的『法人淨買股數』做解讀，"
        "只能引用提供的數字。輸出繁體中文。"
        "若引用外資近5日淨買，請在 cited_foreign_net_5d 填入實算值。"
    )
    prompt = (
        f"股票代號 {stock_id}，基準日 {as_of}。法人淨買（股數，正=買超）：\n"
        f"{json.dumps(feats, ensure_ascii=False, indent=2)}\n\n"
        "請判斷籌碼面方向、給出 score 與 confidence，並列出關鍵觀察與總結。"
    )
    rpt = llm.call_structured(
        model=_model(), system=system, user_prompt=prompt, schema=ChipsReport,
        agent="chips", stock_id=stock_id, as_of=as_of,
    )
    return normalize_scores(rpt) if rpt else rpt, feats


def run_fundamental(stock_id: str, as_of: str) -> tuple[FundamentalReport | None, dict]:
    feats = F.fundamental_features(stock_id, as_of)
    if not feats:
        return None, feats
    system = (
        "你是專業台股基本面分析師。根據提供的月營收數據做解讀，只能引用提供的數字。"
        "輸出繁體中文。若引用月營收年增率，請在 cited_revenue_yoy 填入實算值（小數）。"
    )
    prompt = (
        f"股票代號 {stock_id}，基準日 {as_of}。月營收數據：\n"
        f"{json.dumps(feats, ensure_ascii=False, indent=2)}\n\n"
        "請判斷基本面方向、給出 score 與 confidence，並列出關鍵觀察與總結。"
    )
    rpt = llm.call_structured(
        model=_model(), system=system, user_prompt=prompt, schema=FundamentalReport,
        agent="fundamental", stock_id=stock_id, as_of=as_of,
    )
    return normalize_scores(rpt) if rpt else rpt, feats
