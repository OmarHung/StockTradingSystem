"""驗證層（falsification）：把 LLM 引用的數字與實算值交叉比對。

不符即記錄攔截並「降低該報告信心」，杜絕幻覺決策。這是 LLM_trader 的核心機制。
回傳 (是否通過, 攔截訊息清單, 調整後信心)。
"""
from __future__ import annotations

from src.llm import client as llm

# 各數字的相對容差（考量 LLM 可能四捨五入）
_REL_TOL = 0.05
_ABS_TOL_SHARES = 1000  # 股數的絕對容差


def _mismatch(stated, actual, rel_tol=_REL_TOL, abs_tol=0.0) -> bool:
    if stated is None or actual is None:
        return False  # 未引用則不檢查
    diff = abs(stated - actual)
    return diff > max(abs_tol, abs(actual) * rel_tol)


def validate_technical(report, feats: dict, stock_id: str, as_of: str) -> tuple[bool, list[str], float]:
    flags = []
    if _mismatch(report.cited_adx, feats.get("adx14")):
        flags.append(f"ADX 宣稱 {report.cited_adx} 與實算 {feats.get('adx14')} 不符")
    if _mismatch(report.cited_rsi, feats.get("rsi14")):
        flags.append(f"RSI 宣稱 {report.cited_rsi} 與實算 {feats.get('rsi14')} 不符")
    return _finish("technical", flags, report.confidence, stock_id, as_of)


def validate_chips(report, feats: dict, stock_id: str, as_of: str) -> tuple[bool, list[str], float]:
    flags = []
    if _mismatch(report.cited_foreign_net_5d, feats.get("foreign_net_5d"), abs_tol=_ABS_TOL_SHARES):
        flags.append(
            f"外資5日淨買宣稱 {report.cited_foreign_net_5d} 與實算 {feats.get('foreign_net_5d')} 不符"
        )
    return _finish("chips", flags, report.confidence, stock_id, as_of)


def validate_fundamental(report, feats: dict, stock_id: str, as_of: str) -> tuple[bool, list[str], float]:
    flags = []
    if _mismatch(report.cited_revenue_yoy, feats.get("revenue_yoy")):
        flags.append(
            f"營收年增率宣稱 {report.cited_revenue_yoy} 與實算 {feats.get('revenue_yoy')} 不符"
        )
    if _mismatch(getattr(report, "cited_per", None), feats.get("per")):
        flags.append(
            f"本益比宣稱 {report.cited_per} 與實算 {feats.get('per')} 不符"
        )
    return _finish("fundamental", flags, report.confidence, stock_id, as_of)


def validate_news(report, feats: dict, stock_id: str, as_of: str) -> tuple[bool, list[str], float]:
    flags = []
    actual = feats.get("news_count")
    if report.cited_news_count is not None and actual is not None \
            and int(report.cited_news_count) != int(actual):
        flags.append(
            f"新聞則數宣稱 {report.cited_news_count} 與實際提供 {actual} 不符"
        )
    return _finish("news", flags, report.confidence, stock_id, as_of)


def _finish(agent, flags, confidence, stock_id, as_of):
    if not flags:
        return True, [], confidence
    # 有不符：降信心（每條 -0.3，最低 0）並記錄攔截
    adjusted = max(0.0, confidence - 0.3 * len(flags))
    note = f"[驗證層攔截] {agent}: " + "；".join(flags) + f"（信心 {confidence}→{adjusted}）"
    llm.log_note(f"validator:{agent}", note, stock_id=stock_id, as_of=as_of)
    return False, flags, adjusted
