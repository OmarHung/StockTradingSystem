"""多因子選股引擎：因子 → 橫斷面 z-score 正規化 → 加權綜合分 → Top N。

輸出含每檔的綜合分與各因子分數拆解，供 WebUI 選股頁展示與 Phase 2 交給 LLM 精選。
"""
from __future__ import annotations

import pandas as pd

from src.config import get_settings
from src.data import query as q
from src.logging_setup import get_logger
from src.screener import factors as F

log = get_logger(__name__)


def _zscore(s: pd.Series) -> pd.Series:
    """橫斷面標準化；全 NaN 或零變異回 0，NaN 以 0（中性）填補。"""
    valid = s.dropna()
    if valid.empty or valid.std(ddof=0) == 0:
        return pd.Series(0.0, index=s.index)
    z = (s - valid.mean()) / valid.std(ddof=0)
    return z.fillna(0.0)


def run_screener(as_of: str, universe: list[str] | None = None, cfg=None) -> pd.DataFrame:
    """對 as_of 日執行選股，回傳排名後的 DataFrame（含綜合分與因子分拆解）。"""
    cfg = cfg or get_settings()
    sc = cfg["screener"]
    universe = universe or q.all_stock_ids()

    raw = F.compute_factors(
        stock_ids=universe,
        as_of=as_of,
        momentum_lookback=sc["momentum_lookback"],
        chips_lookback=sc["chips_lookback"],
        min_avg_turnover=sc["min_avg_turnover"],
    )
    if raw.empty:
        log.warning("as_of=%s 無任何股票通過因子/流動性條件", as_of)
        return raw

    weights: dict[str, float] = sc["weights"]
    total_w = sum(abs(w) for w in weights.values()) or 1.0

    score = pd.Series(0.0, index=raw.index)
    for factor, w in weights.items():
        if factor not in raw.columns:
            log.warning("因子 %s 不在資料中，跳過", factor)
            continue
        z = _zscore(raw[factor])
        raw[f"z_{factor}"] = z
        score += w * z
    raw["score"] = score / total_w

    ranked = raw.sort_values("score", ascending=False).reset_index(drop=True)
    ranked.insert(0, "rank", ranked.index + 1)

    # 附上股票名稱
    names = q.list_stocks()[["stock_id", "stock_name", "industry_category"]]
    ranked = ranked.merge(names, on="stock_id", how="left")

    top_n = sc["top_n"]
    log.info("as_of=%s 選股完成：%d 檔入池，取前 %d", as_of, len(ranked), top_n)
    return ranked.head(top_n)
