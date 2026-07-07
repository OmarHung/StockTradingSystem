"""多因子選股引擎：股票池 → 因子 → 橫斷面 z-score 正規化 → 加權綜合分 → Top N。

股票池兩種模式（settings.yaml screener.universe）：
- all（預設，業界式漏斗）：全市場，流動性當「門檻」過濾（min_avg_turnover），
  再以動能/籌碼/爆量倍數/營收等因子加權排名取 Top N
- volume_top：只看當日成交量前 N 高的熱門股（池小、z-score 統計意義弱，備用）

輸出含每檔的綜合分與各因子分數拆解，供 WebUI 選股頁展示與 Phase 2 交給 LLM 精選。
"""
from __future__ import annotations

import pandas as pd

from src.config import get_settings
from src.data import query as q
from src.logging_setup import get_logger
from src.screener import factors as F

log = get_logger(__name__)


def _zscore(s: pd.Series, winsor_pct: float = 0.01) -> pd.Series:
    """橫斷面標準化；全 NaN 或零變異回 0，NaN 以 0（中性）填補。

    標準化前先 winsorize（1%/99% 分位截尾）——單一極端值（如營收年增率
    874 倍的轉機股）會把自己的 z 分拉到十幾倍、淹沒其他所有因子。
    """
    valid = s.dropna()
    if valid.empty or valid.std(ddof=0) == 0:
        return pd.Series(0.0, index=s.index)
    if len(valid) >= 20:  # 樣本太少時分位數截尾沒有意義
        lo, hi = valid.quantile(winsor_pct), valid.quantile(1 - winsor_pct)
        s = s.clip(lo, hi)
        valid = s.dropna()
        if valid.std(ddof=0) == 0:
            return pd.Series(0.0, index=s.index)
    z = (s - valid.mean()) / valid.std(ddof=0)
    return z.fillna(0.0)


def run_screener(as_of: str, universe: list[str] | None = None, cfg=None,
                 progress=None) -> pd.DataFrame:
    """對 as_of 日執行選股，回傳排名後的 DataFrame（含綜合分與因子分拆解）。

    progress(stage, current, total)：進度回呼（供 WebUI 實時顯示），可為 None。
    """
    cfg = cfg or get_settings()
    sc = cfg["screener"]
    if progress:
        progress("載入股票池", 0, 0)
    if universe is None:
        mode = sc.get("universe", "all")
        if mode == "volume_top":
            vol_n = int(sc.get("volume_top_n", 10))
            universe = q.top_volume_ids(as_of, vol_n)
            log.info("as_of=%s 股票池＝成交量前 %d 高：%s", as_of, vol_n, universe)
        else:
            universe = q.all_stock_ids()

    raw = F.compute_factors(
        stock_ids=universe,
        as_of=as_of,
        momentum_lookback=sc["momentum_lookback"],
        chips_lookback=sc["chips_lookback"],
        min_avg_turnover=sc["min_avg_turnover"],
        progress=progress,
    )
    if raw.empty:
        log.warning("as_of=%s 無任何股票通過因子/流動性條件", as_of)
        return raw

    if progress:
        progress(f"正規化與排名（{len(raw)} 檔通過流動性條件）", 0, 0)
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
