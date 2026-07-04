"""shioaji（永豐金）備援資料源：FinMind 額度用罄時接手回補股價日 K。

核心：`api.daily_quotes(date)` 一次回傳「全市場」當日行情——按日呼叫而非按檔，
額度效率極高（補 250 個交易日 = 250 次呼叫，涵蓋所有股票）。

需求：.env 設 SJ_API_KEY / SJ_SEC_KEY（永豐 API 金鑰，simulation 登入即可查行情）。
沒裝套件或沒金鑰時 available() 回 False，呼叫端優雅降級。
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

from src.data import database as db
from src.logging_setup import get_logger

log = get_logger(__name__)

_api = None       # 模組級單例（登入一次）
_api_mode = None  # 登入時的環境；設定改變則重新登入


def current_env() -> str:
    """讀 settings.yaml 的券商環境：simulation（預設）/ production。"""
    from src.config import get_settings
    try:
        get_settings.cache_clear()  # 設定頁可能剛改過
        return (get_settings().get("shioaji") or {}).get("environment", "simulation")
    except Exception:  # noqa: BLE001
        return "simulation"


def available() -> bool:
    """套件已安裝且金鑰已設定。"""
    try:
        import shioaji  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("SJ_API_KEY")) and bool(os.getenv("SJ_SEC_KEY"))


def _login():
    global _api, _api_mode
    mode = current_env()
    if _api is not None and _api_mode == mode:
        return _api
    if _api is not None:  # 環境切換 → 先登出舊連線
        try:
            _api.logout()
        except Exception:  # noqa: BLE001
            pass
        _api = None
    import shioaji as sj

    simulation = mode != "production"
    api = sj.Shioaji(simulation=simulation)
    api.login(os.environ["SJ_API_KEY"], os.environ["SJ_SEC_KEY"])
    _api, _api_mode = api, mode
    log.info("shioaji 已登入（%s）", "simulation 模擬" if simulation else "⚠️ PRODUCTION 正式環境")
    return _api


def fetch_daily_for_date(conn, date_str: str, wanted_ids: set[str]) -> int:
    """抓某交易日全市場行情，篩 wanted_ids 寫入 price_daily。回傳寫入列數。

    DailyQuotes 是「欄向量」結構（column-oriented）：.Code/.Open/.Close 各是
    一條等長陣列，不能按列疊代（d[0] 會炸 'int' object is not 'str'）。
    量能單位已是「股」（實測與 FinMind 完全一致，勿再換算）。
    """
    api = _login()
    day = dt.date.fromisoformat(date_str)
    q = api.daily_quotes(date=day)
    codes = list(getattr(q, "Code", []) or [])
    if not codes:
        return 0

    df = pd.DataFrame({
        "stock_id": [str(c) for c in codes],
        "open": list(q.Open),
        "high": list(q.High),
        "low": list(q.Low),
        "close": list(q.Close),
        "volume": list(q.Volume),
        "trading_money": list(q.Amount),
        "trading_turnover": list(q.Transaction),
    })
    df = df[df["stock_id"].isin(wanted_ids) & (df["close"].astype(float) > 0)].copy()
    if df.empty:
        return 0
    df["date"] = date_str
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")

    n = db.upsert_dataframe(conn, "price_daily", df)
    # 更新每檔 fetch_log 已補範圍
    now = dt.datetime.now().isoformat(timespec="seconds")
    for sid in df["stock_id"].unique():
        db.merge_range(conn, "price_daily", sid, date_str, date_str, now)
    return n


def fetch_disposition(conn) -> int:
    """處置股第二源：api.punish()（欄向量）。與 TWSE 官方名單雙源並用（upsert 去重）。"""
    api = _login()
    p = api.punish()
    codes = list(getattr(p, "code", []) or [])
    if not codes:
        return 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    df = pd.DataFrame({
        "stock_id": [str(c) for c in codes],
        "market": "shioaji",
        "name": "",
        "reason": list(p.description),
        "period_start": [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in p.start_date],
        "period_end": [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in p.end_date],
        "fetched_at": now,
    })
    df = df[df["stock_id"] != ""]
    return db.upsert_dataframe(conn, "disposition", df)


# 排行掃描器類型對照（供 API /scanner）
# 注意：實測本版 shioaji 的 ascending 語義與文件相反——
# ascending=True 才是「榜首在前」（台積電成交額登頂驗證），勿改回。
_SCANNER_KINDS = {
    "change_pct_up":   ("ChangePercentRank", True),   # 漲幅排行
    "change_pct_down": ("ChangePercentRank", False),  # 跌幅排行
    "amount":          ("AmountRank", True),          # 成交金額排行
    "volume":          ("VolumeRank", True),          # 成交量排行
}


def get_scanners(kind: str, count: int = 20) -> list[dict]:
    """即時排行（漲幅/跌幅/成交值/成交量）。回傳 list[dict] 供 API 直接輸出。"""
    import shioaji as sj_mod

    if kind not in _SCANNER_KINDS:
        raise ValueError(f"未知排行類型 {kind}（可選：{','.join(_SCANNER_KINDS)}）")
    type_name, ascending = _SCANNER_KINDS[kind]
    api = _login()
    st_enum = getattr(sj_mod, "ScannerType", None) or sj_mod.constant.ScannerType
    scans = api.scanners(
        scanner_type=getattr(st_enum, type_name),
        ascending=ascending, count=count,
    )
    out = []
    for s in scans or []:
        out.append({
            "code": s.code, "name": s.name, "close": s.close,
            "change_price": s.change_price,
            "change_pct": round(s.change_price / (s.close - s.change_price) * 100, 2)
                          if (s.close - s.change_price) else 0.0,
            "total_volume": s.total_volume, "total_amount": s.total_amount,
            "date": s.date,
        })
    return out
