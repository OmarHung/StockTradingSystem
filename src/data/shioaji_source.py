"""shioaji（永豐金）K 線資料主源：日 K、指數、1 分 K 歷史與即時行情。

- 個股日 K：`api.daily_quotes(date)` 一次回傳「全市場」當日行情——按日呼叫而非
  按檔，額度效率極高（補 250 個交易日 = 250 次呼叫，涵蓋所有股票）。
- 指數日 K（TAIEX/TPEx）：daily_quotes 不含指數，改抓 1 分 K 聚合（fetch_index_daily）。
- 1 分 K：`api.kbars`（歷史最早 2020-03-02），開圖時 ensure_minute_bars 自動補缺；
  盤中即時 tick 合成見 src/realtime.py。

FinMind 已完全退出 K 線路徑（額度留給籌碼/基本面資料集）。

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


def get_api():
    """取得已登入的 shioaji api 單例（供即時行情服務 src/realtime.py 使用）。"""
    return _login()


def get_contract(api, stock_id: str):
    """代號 → shioaji 合約。支援個股/ETF 與指數（TAIEX=加權 001、TPEx=櫃買 101）。"""
    if stock_id == "TAIEX":
        return api.Contracts.Indexs.TSE["001"]
    if stock_id == "TPEx":
        return api.Contracts.Indexs.OTC["101"]
    return api.Contracts.Stocks[stock_id]


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


# ---- 1 分 K（歷史回補；即時合成見 src/realtime.py）----
# 收盤競價 13:30 撮合完、資料落定約需幾分鐘 → 13:35 後抓到的當日資料視為完整
_SESSION_SETTLED = "13:35:00"
KBARS_EARLIEST = "2020-03-02"   # shioaji 歷史 K 線最早日期（股票/指數）


def fetch_kbars_df(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """抓 [start, end] 的 1 分 K（含當日盤中已成交部分）。

    ts 為該分鐘「結束」時間（台股看盤慣例：首根 09:01、收盤競價 13:30，
    實測 shioaji 原生即此語義，直接沿用不位移）。volume 單位＝張（指數為市場總量）。
    """
    api = _login()
    contract = get_contract(api, stock_id)
    if contract is None:
        return pd.DataFrame()
    kb = api.kbars(contract, start=max(start, KBARS_EARLIEST), end=end)
    ts = list(getattr(kb, "ts", []) or [])
    if not ts:
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts": pd.to_datetime(pd.Series(ts)).dt.strftime("%Y-%m-%d %H:%M:%S"),
        "open": list(kb.Open), "high": list(kb.High),
        "low": list(kb.Low), "close": list(kb.Close),
        "volume": pd.to_numeric(pd.Series(list(kb.Volume)), errors="coerce")
                    .fillna(0).astype("int64"),
        "amount": list(kb.Amount),
    })
    df["stock_id"] = stock_id
    return df


def ensure_minute_bars(conn, stock_id: str, days: int = 30) -> int:
    """開圖自動回補：把該檔 kbar_1min 補到「現在」，最早回溯 days 天。回傳寫入列數。

    進度記錄 fetch_log(dataset='kbar_1min')，first/last 為日期。「最後一天」永遠
    重抓（可能只抓到盤中一半）；當日收盤後（13:35）抓過、或 20 秒內剛抓過則跳過，
    避免面板頻繁重掛時重複打 API。
    """
    today = dt.date.today().isoformat()
    now = dt.datetime.now()
    want_start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    first, last = db.get_range(conn, "kbar_1min", stock_id)
    row = conn.execute(
        "SELECT updated_at FROM fetch_log WHERE dataset='kbar_1min' AND stock_id=?",
        (stock_id,)).fetchone()
    updated_at = (row[0] if row else None) or ""

    segments: list[tuple[str, str]] = []
    if last is None:
        segments.append((want_start, today))
    else:
        if first and first > want_start:   # 使用者拉長回看範圍 → 往前補歷史
            segments.append((want_start, first))
        # settled = 上次抓取時 last 當日已收盤落定（該日資料完整，不必重抓）
        settled = updated_at >= f"{last}T{_SESSION_SETTLED}"
        recent = updated_at >= (now - dt.timedelta(seconds=20)).isoformat(timespec="seconds")
        if last < today:
            seg_from = ((dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()
                        if settled else last)
            segments.append((seg_from, today))
        elif not (settled or recent):
            segments.append((last, today))  # 今天已抓過但仍在盤中 → 重抓（upsert 冪等）

    total = 0
    for seg_start, seg_end in segments:
        df = fetch_kbars_df(stock_id, seg_start, seg_end)
        total += db.upsert_dataframe(conn, "kbar_1min", df)
        db.merge_range(conn, "kbar_1min", stock_id, seg_start, seg_end,
                       now.isoformat(timespec="seconds"))
    if total:
        conn.commit()
    return total


def fetch_index_daily(conn, start: str, end: str) -> int:
    """指數日 K（TAIEX 加權 / TPEx 櫃買）：抓 1 分 K 按日聚合寫入 price_daily。

    daily_quotes 不含指數，故走 kbars；歷史最早 2020-03-02，更早期間略過
    （既有資料庫已有舊資料則保留）。分 K Volume 單位為張 → ×1000 存股數，
    與既有 FinMind 指數列一致（實測 TAIEX 每日 1.5e10 股同量級）。
    """
    total = 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    for sid in ("TAIEX", "TPEx"):
        cur = dt.date.fromisoformat(max(start, KBARS_EARLIEST))
        end_d = dt.date.fromisoformat(end)
        frames = []
        while cur <= end_d:  # kbars 範圍查詢分段（一段約一季，避免單次回應過大）
            seg_end = min(cur + dt.timedelta(days=89), end_d)
            frames.append(fetch_kbars_df(sid, cur.isoformat(), seg_end.isoformat()))
            cur = seg_end + dt.timedelta(days=1)
        m = pd.concat(frames) if frames else pd.DataFrame()
        if m.empty:
            continue
        m["date"] = m["ts"].str[:10]
        g = m.groupby("date")
        daily = pd.DataFrame({
            "open": g["open"].first(), "high": g["high"].max(),
            "low": g["low"].min(), "close": g["close"].last(),
            "volume": g["volume"].sum().astype("int64") * 1000,   # 張 → 股
            "trading_money": g["amount"].sum(),
        }).reset_index()
        daily["stock_id"] = sid
        total += db.upsert_dataframe(conn, "price_daily", daily)
        db.merge_range(conn, "price_daily", sid,
                       str(daily["date"].min()), str(daily["date"].max()), now)
        conn.commit()
    return total


def list_tradable_ids() -> set[str]:
    """現行可交易股票代號全集（TSE/OTC/OES 合約）。

    券商合約僅含未下市證券，作為下市判定的權威名單；
    回傳空集合視為抓取失敗，呼叫端不得據以標記下市。
    登入後合約檔為非同步下載，須等 Fetched 再迭代，否則清單不完整會誤標。
    """
    import time
    api = _login()
    for _ in range(60):
        status = str(getattr(api.Contracts, "status", "")).split(".")[-1]
        if status == "Fetched":
            break
        time.sleep(0.5)
    else:
        log.warning("shioaji 合約檔下載逾時（status=%s）", status)
        return set()
    ids: set[str] = set()
    for exchange in api.Contracts.Stocks:
        for contract in exchange:
            ids.add(str(contract.code))
    return ids


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


def fetch_snapshots(codes: list[str]) -> dict[str, dict]:
    """持倉股票即時快照（盤中停損監控用）。

    回傳 {code: {close, low, high, ts_date}}；ts_date 供判斷是否為今日行情
    （休市日快照是前一交易日的，呼叫端據此判斷今天沒開盤）。
    單次最多 500 檔、額度 50 req/5s——輪詢幾檔持倉綽綽有餘。
    """
    import datetime as _dt

    if not codes:
        return {}
    api = _login()
    contracts = [api.Contracts.Stocks[c] for c in codes
                 if api.Contracts.Stocks[c] is not None]
    if not contracts:
        return {}
    out = {}
    for s in api.snapshots(contracts) or []:
        ts_date = _dt.datetime.fromtimestamp(s.ts / 1e9).date().isoformat() if s.ts else None
        out[s.code] = {"close": float(s.close or 0), "low": float(s.low or 0),
                       "high": float(s.high or 0), "ts_date": ts_date}
    return out
