"""四位資料型分析師：技術面、籌碼面、基本面、新聞面。

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
    NewsReport,
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


def run_news(stock_id: str, as_of: str) -> tuple[NewsReport | None, dict]:
    """新聞面分析師。新聞按需抓取（只對進入深度分析的個股，省 FinMind 額度）；

    無新聞（冷門股/抓取失敗）回 (None, {})，管線自動跳過此報告。
    """
    from src.data import fetchers

    ncfg = get_settings().get("news") or {}
    fetchers.ensure_news(stock_id, as_of, lookback_days=int(ncfg.get("lookback_days", 10)))
    feats = F.news_features(stock_id, as_of)
    if not feats:
        return None, feats
    system = (
        "你是專業台股新聞分析師。根據提供的『新聞標題清單』判讀消息面多空，"
        "只能依據清單內容，不得腦補未提供的新聞或細節。輸出繁體中文。"
        "特別留意『政策題材』：政府政策、法規鬆綁、補助、國家隊、公共建設、"
        "國防軍工等題材常是飆股啟動的催化劑——若存在請把 policy_driven 設為 true "
        "並在 key_themes 標註。也要辨識利空（處分、糾紛、訂單流失、調查）。"
        "注意：只有標題沒有內文，對單則標題的解讀要保守；多則同向新聞才可提高信心。"
        "cited_news_count 請照抄輸入的 news_count。"
    )
    prompt = (
        f"股票代號 {stock_id}，基準日 {as_of}。近 {feats['lookback_days']} 日新聞如下：\n"
        f"{json.dumps(feats, ensure_ascii=False, indent=2)}\n\n"
        "請判斷新聞面方向（bullish/neutral/bearish）、是否政策題材驅動（policy_driven）、"
        "給出 score[-1,1] 與 confidence[0,1]，並列出主題標籤、關鍵觀察與總結。"
    )
    rpt = llm.call_structured(
        model=_model(), system=system, user_prompt=prompt, schema=NewsReport,
        agent="news", stock_id=stock_id, as_of=as_of,
    )
    return normalize_scores(rpt) if rpt else rpt, feats


def run_fundamental(stock_id: str, as_of: str) -> tuple[FundamentalReport | None, dict]:
    feats = F.fundamental_features(stock_id, as_of)
    if not feats:
        return None, feats
    system = (
        "你是專業台股基本面分析師。根據提供的月營收與估值數據做解讀，只能引用提供的數字。"
        "輸出繁體中文。"
        "單位注意：revenue_yoy 是『小數』（1.0 = +100%），revenue_yoy_pct 是同一數字的"
        "百分比表達，兩者互為對照——建設股等認列不均的產業可能出現數百倍的極端年增率，"
        "此時 revenue_yoy 會是幾百的小數，這是真實數據不是百分比，勿自行除以100。"
        "cited_revenue_yoy 請『原封不動照抄 revenue_yoy 的值』，禁止任何換算；"
        "若引用本益比，請在 cited_per 填入實算值。"
        "估值欄位：per=本益比（虧損公司為 null）、pbr=股價淨值比、"
        "dividend_yield_pct=殖利率(%)、per_percentile_1y=本益比近一年百分位"
        "（0=一年最便宜，1=一年最貴）。"
        "若有 next_ex_date（即將除權息日）請納入考量：除權息日股價會跳空調整，"
        "臨近進場需留意息值大小與填息能力。"
    )
    prompt = (
        f"股票代號 {stock_id}，基準日 {as_of}。月營收與估值數據：\n"
        f"{json.dumps(feats, ensure_ascii=False, indent=2)}\n\n"
        "請綜合成長性（營收動能）與估值合理性（本益比/淨值比/殖利率及歷史位階），"
        "判斷基本面方向、給出 score 與 confidence，並列出關鍵觀察與總結。"
    )
    rpt = llm.call_structured(
        model=_model(), system=system, user_prompt=prompt, schema=FundamentalReport,
        agent="fundamental", stock_id=stock_id, as_of=as_of,
    )
    return normalize_scores(rpt) if rpt else rpt, feats
