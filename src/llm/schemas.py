"""LLM 結構化輸出 schema（Pydantic）。

分析師的 `cited_*` 欄位是「LLM 引用的關鍵數字」，供驗證層與實算值交叉比對
（falsification）——不符即駁回或降信心，杜絕幻覺決策。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Signal = Literal["bullish", "neutral", "bearish"]

_SCORE_DESC = (
    "方向分數，必須是 -1.0 到 1.0 之間的小數（不是百分比）。"
    "例如強烈看多=0.8、中性=0.0、強烈看空=-0.8。看空必須為負、看多必須為正，"
    "須與 signal 一致。絕對值不得超過 1。"
)
_CONF_DESC = (
    "信心，必須是 0.0 到 1.0 之間的小數（不是百分比）。例如高信心=0.85，不是 85。"
)


class TechnicalReport(BaseModel):
    """技術面分析師輸出。"""
    signal: Signal
    score: float = Field(description=_SCORE_DESC)
    confidence: float = Field(description=_CONF_DESC)
    cited_adx: float | None = Field(default=None, description="你引用的 ADX 值（若提及趨勢強度）")
    cited_rsi: float | None = Field(default=None, description="你引用的 RSI14 值")
    key_points: list[str] = Field(description="3-5 條關鍵觀察")
    summary: str = Field(description="一段技術面總結（繁體中文）")


class ChipsReport(BaseModel):
    """籌碼面分析師輸出。"""
    signal: Signal
    score: float = Field(description=_SCORE_DESC)
    confidence: float = Field(description=_CONF_DESC)
    cited_foreign_net_5d: float | None = Field(
        default=None, description="你引用的『外資近5日淨買股數』"
    )
    key_points: list[str]
    summary: str


class FundamentalReport(BaseModel):
    """基本面分析師輸出。"""
    signal: Signal
    score: float = Field(description=_SCORE_DESC)
    confidence: float = Field(description=_CONF_DESC)
    cited_revenue_yoy: float | None = Field(
        default=None,
        description="照抄輸入數據 revenue_yoy 的原值（小數，1.0=+100%），禁止換算。"
                    "極端產業可能出現數百的值，照抄即可。"
    )
    cited_per: float | None = Field(
        default=None, description="你引用的『本益比』實算值（如 18.5；未引用留空）"
    )
    key_points: list[str]
    summary: str


class NewsReport(BaseModel):
    """新聞面分析師輸出。"""
    signal: Signal
    score: float = Field(description=_SCORE_DESC)
    confidence: float = Field(description=_CONF_DESC)
    policy_driven: bool = Field(
        description="是否有政府政策/法規/補助/國家隊/公共建設等『政策題材』驅動的利多或利空"
    )
    cited_news_count: int | None = Field(
        default=None, description="照抄輸入數據 news_count 的值（你實際看到的新聞則數）"
    )
    key_themes: list[str] = Field(
        description="1-4 個新聞主題標籤（如：政策利多、接單動能、財報、經營糾紛）"
    )
    key_points: list[str] = Field(description="3-5 條關鍵觀察")
    summary: str = Field(description="一段新聞面總結（繁體中文）")


class TradePlan(BaseModel):
    """交易員 Agent 對單一標的的最終決策與進出場計畫。"""
    action: Literal["buy", "hold", "avoid"]
    action_score: float = Field(description=_SCORE_DESC)
    confidence: float = Field(description=_CONF_DESC)
    entry_low: float | None = Field(default=None, description="建議進場價區間下緣")
    entry_high: float | None = Field(default=None, description="建議進場價區間上緣")
    stop_loss: float | None = Field(default=None, description="停損價")
    target_price: float | None = Field(default=None, description="目標價")
    reward_risk: float | None = Field(default=None, description="報酬風險比 (目標-進場)/(進場-停損)")
    rationale: str = Field(description="決策理由（繁體中文，需綜合各分析師觀點）")
    risks: list[str] = Field(description="主要風險提示")


def normalize_scores(report):
    """保底夾取：把 LLM 可能誤輸出的百分比/超範圍值修回 score∈[-1,1]、confidence∈[0,1]。

    就地修改並回傳同一物件。分數看似百分比（|x|>1）時除以 100 再夾取；
    修正後強制 score 正負號與 signal 一致（避免 bearish 卻給正分的矛盾）。
    """
    for field in ("score", "action_score"):
        if hasattr(report, field):
            v = getattr(report, field)
            if abs(v) > 1:
                v = v / 100.0
            setattr(report, field, max(-1.0, min(1.0, v)))
    if hasattr(report, "confidence"):
        c = report.confidence
        if c > 1:
            c = c / 100.0
        report.confidence = max(0.0, min(1.0, c))
    # 分數符號與 signal 對齊（僅 analyst 報告有 signal）
    sig = getattr(report, "signal", None)
    if sig and hasattr(report, "score"):
        s = report.score
        if sig == "bearish" and s > 0:
            report.score = -abs(s)
        elif sig == "bullish" and s < 0:
            report.score = abs(s)
    return report
