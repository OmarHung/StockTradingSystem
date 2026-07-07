"""高層抓取器：呼叫 FinMind → 欄位轉換 → 寫入資料庫。

每個 fetch_* 函式都負責一種 dataset，並更新 fetch_log 以支援增量更新。
欄位對照依 FinMind API v4 實際回傳（見各函式內註解）。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from src.data import database as db
from src.data.finmind_client import FinMindClient
from src.logging_setup import get_logger

log = get_logger(__name__)


# ---- 股票清單 ----
def fetch_stock_info(client: FinMindClient, conn) -> pd.DataFrame:
    """抓全市場股票清單並寫入 stock_info。回傳寫入的 DataFrame。"""
    raw = client.get_dataset("TaiwanStockInfo")
    if raw.empty:
        log.warning("TaiwanStockInfo 回傳空資料")
        return raw
    # 同一 stock_id 可能有多列（不同日期），保留最新一列
    raw = raw.sort_values("date").drop_duplicates("stock_id", keep="last")
    df = raw[["stock_id", "stock_name", "industry_category", "type", "date"]].copy()
    db.upsert_dataframe(conn, "stock_info", df)
    log.info("stock_info 寫入 %d 檔", len(df))
    return df


# ---- 日 K：已全面改 shioaji（src/data/shioaji_source.py），FinMind 不再提供 K 線 ----


# ---- 三大法人 ----
def fetch_institutional(client: FinMindClient, conn, stock_id: str, start: str, end: str) -> int:
    raw = client.get_dataset(
        "TaiwanStockInstitutionalInvestorsBuySell", data_id=stock_id, start_date=start, end_date=end
    )
    if raw.empty:
        return 0
    df = raw[["stock_id", "date", "name", "buy", "sell"]].copy()
    n = db.upsert_dataframe(conn, "institutional", df)
    _update_log(conn, "institutional", stock_id, df["date"])
    return n


# ---- 融資融券 ----
_MARGIN_MAP = {
    "MarginPurchaseBuy": "margin_purchase_buy",
    "MarginPurchaseSell": "margin_purchase_sell",
    "MarginPurchaseTodayBalance": "margin_purchase_balance",
    "ShortSaleBuy": "short_sale_buy",
    "ShortSaleSell": "short_sale_sell",
    "ShortSaleTodayBalance": "short_sale_balance",
}


def fetch_margin(client: FinMindClient, conn, stock_id: str, start: str, end: str) -> int:
    raw = client.get_dataset(
        "TaiwanStockMarginPurchaseShortSale", data_id=stock_id, start_date=start, end_date=end
    )
    if raw.empty:
        return 0
    df = raw.rename(columns=_MARGIN_MAP)
    n = db.upsert_dataframe(conn, "margin", df)
    _update_log(conn, "margin", stock_id, df["date"])
    return n


# ---- 除權息 ----
def fetch_dividend(client: FinMindClient, conn, stock_id: str, start: str, end: str) -> int:
    raw = client.get_dataset(
        "TaiwanStockDividendResult", data_id=stock_id, start_date=start, end_date=end
    )
    if raw.empty:
        return 0
    df = raw.rename(columns={
        "stock_and_cache_dividend": "dividend",
        "stock_or_cache_dividend": "kind",
    })[["stock_id", "date", "before_price", "after_price", "dividend", "kind"]]
    n = db.upsert_dataframe(conn, "dividend", df)
    _update_log(conn, "dividend", stock_id, df["date"])
    return n


# ---- 處置股（TWSE/TPEx 官方 OpenAPI，全市場快照，免 token）----
def _roc_to_iso(roc: str) -> str | None:
    """民國日期 '1150703' 或 '115/07/03' → '2026-07-03'。"""
    s = roc.strip().replace("/", "")
    if len(s) != 7 or not s.isdigit():
        return None
    return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"


def fetch_disposition(conn) -> int:
    """抓 TWSE + TPEx 當前處置股名單。全市場一次，冪等 upsert。

    走 twse_source._session：TPEx 憑證缺 SKI 擴展，Python 3.13 預設
    VERIFY_X509_STRICT 會讓裸 requests 對 tpex 握手失敗。
    """
    from src.data.twse_source import _session as requests  # noqa: N813

    now = dt.datetime.now().isoformat(timespec="seconds")
    rows: list[dict] = []

    try:
        r = requests.get("https://openapi.twse.com.tw/v1/announcement/punish",
                         timeout=20, headers={"accept": "application/json"})
        for item in r.json():
            period = (item.get("DispositionPeriod") or "").replace("～", "~")
            parts = period.split("~")
            rows.append({
                "stock_id": item.get("Code", "").strip(),
                "market": "twse", "name": item.get("Name", ""),
                "reason": item.get("ReasonsOfDisposition", ""),
                "period_start": _roc_to_iso(parts[0]) if parts else None,
                "period_end": _roc_to_iso(parts[1]) if len(parts) > 1 else None,
                "fetched_at": now,
            })
    except Exception as e:  # noqa: BLE001
        log.error("TWSE 處置股抓取失敗：%s", e)

    try:
        r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
                         timeout=20, headers={"accept": "application/json"})
        for item in r.json():
            period = (item.get("DispositionPeriod") or "").replace("～", "~")
            parts = period.split("~")
            rows.append({
                "stock_id": item.get("SecuritiesCompanyCode", "").strip(),
                "market": "tpex", "name": item.get("CompanyName", ""),
                "reason": item.get("DispositionReasons", ""),
                "period_start": _roc_to_iso(parts[0]) if parts else None,
                "period_end": _roc_to_iso(parts[1]) if len(parts) > 1 else None,
                "fetched_at": now,
            })
    except Exception as e:  # noqa: BLE001
        log.error("TPEx 處置股抓取失敗：%s", e)

    df = pd.DataFrame([r for r in rows if r["stock_id"] and r["period_start"]])
    if df.empty:
        return 0
    n = db.upsert_dataframe(conn, "disposition", df)
    log.info("處置股名單更新：%d 筆", n)
    return n


# ---- 月營收 ----
def fetch_month_revenue(client: FinMindClient, conn, stock_id: str, start: str, end: str) -> int:
    raw = client.get_dataset(
        "TaiwanStockMonthRevenue", data_id=stock_id, start_date=start, end_date=end
    )
    if raw.empty:
        return 0
    df = raw[["stock_id", "date", "revenue_year", "revenue_month", "revenue"]].copy()
    n = db.upsert_dataframe(conn, "month_revenue", df)
    # 月營收以公告日期記進度
    _update_log(conn, "month_revenue", stock_id, df["date"])
    return n


# ---- 個股新聞 ----
def fetch_news_day(client: FinMindClient, conn, stock_id: str, day: str) -> int:
    """抓單檔單日新聞。TaiwanStockNews 資料量大，API 限制一次只回一天
    （end_date 必須留空），所以只能逐日呼叫。"""
    raw = client.get_dataset("TaiwanStockNews", data_id=stock_id, start_date=day)
    if raw.empty:
        return 0
    df = raw.copy()
    df["published_at"] = df["date"].astype(str)
    df["date"] = df["published_at"].str[:10]
    df = df.rename(columns={"link": "url"})
    cols = [c for c in ("stock_id", "date", "published_at", "title", "source", "url")
            if c in df.columns]
    df = df[cols].dropna(subset=["title"]).drop_duplicates(["stock_id", "date", "title"])
    n = db.upsert_dataframe(conn, "news", df)
    _update_log(conn, "news", stock_id, df["date"])
    return n


def ensure_news(stock_id: str, as_of: str, lookback_days: int = 10) -> None:
    """按需抓取單檔近期新聞（只對進入深度分析的個股，逐日呼叫）。

    以 fetch_log(dataset='news_check') 記錄「已檢查到哪天」——即使某天無新聞
    也算檢查過，下次只補新的日子，避免重跑管線時反覆空查。
    抓取失敗（額度/網路）只記警告不拋出，失敗當天起不標記（下次續抓），
    新聞分析師以資料庫既有內容（可能為空）繼續。
    """
    from src.config import get_settings

    cfg = get_settings()
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db.connect(cfg.db_path) as conn:
        _, checked_to = db.get_range(conn, "news_check", stock_id)
        window_start = (dt.date.fromisoformat(as_of)
                        - dt.timedelta(days=lookback_days)).isoformat()
        # 要補的日子 = 窗口起點 ~ as_of，其中已檢查過的（<= checked_to）跳過
        first = window_start
        if checked_to and checked_to >= as_of:
            return
        if checked_to and checked_to >= window_start:
            first = (dt.date.fromisoformat(checked_to) + dt.timedelta(days=1)).isoformat()

        fm = cfg.finmind
        client = FinMindClient(
            base_url=fm["base_url"], token=cfg.finmind_token,
            request_interval_sec=fm["request_interval_sec"], max_retries=fm["max_retries"],
        )
        total, day = 0, dt.date.fromisoformat(first)
        end = dt.date.fromisoformat(as_of)
        while day <= end:
            try:
                total += fetch_news_day(client, conn, stock_id, day.isoformat())
            except Exception as e:  # noqa: BLE001 — 額度用罄/網路錯：保留進度下次續抓
                log.warning("新聞抓取 %s %s 失敗（已抓 %d 則，下次續抓）：%s",
                            stock_id, day, total, e)
                break
            # 逐日推進檢查標記：中途失敗也不會重抓已完成的日子
            db.merge_range(conn, "news_check", stock_id, day.isoformat(), day.isoformat(), now)
            day += dt.timedelta(days=1)
        if total:
            log.info("新聞抓取 %s：%d 則（%s ~ %s）", stock_id, total, first, as_of)


def _update_log(conn, dataset: str, stock_id: str, dates: pd.Series) -> None:
    if dates.empty:
        return
    db.merge_range(
        conn, dataset, stock_id,
        str(dates.min()), str(dates.max()),
        dt.datetime.now().isoformat(timespec="seconds"),
    )


def mark_delisted(conn) -> int:
    """以 shioaji 可交易合約為準，標記 stock_info 的下市股。回傳本次新標記檔數。

    只檢查純數字代號（指數等特殊代號不動）；重新上市者會自動解除標記。
    合約清單抓不到（空集合）時不動作，避免誤殺全表。
    """
    from src.data import shioaji_source
    if not shioaji_source.available():
        return 0
    try:
        tradable = shioaji_source.list_tradable_ids()
    except Exception as e:  # noqa: BLE001
        log.warning("shioaji 合約清單抓取失敗，跳過下市標記：%s", e)
        return 0
    if len(tradable) < 2000:   # 全市場股票+ETF 約 2800 檔，過少＝清單不完整
        log.warning("shioaji 合約清單過少（%d 檔），跳過下市標記", len(tradable))
        return 0
    rows = conn.execute("SELECT stock_id, COALESCE(delisted,0) FROM stock_info").fetchall()
    newly = 0
    for sid, old in rows:
        if not sid.isdigit():
            continue
        flag = 0 if sid in tradable else 1
        if flag != old:
            conn.execute("UPDATE stock_info SET delisted=? WHERE stock_id=?", (flag, sid))
            newly += flag
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM stock_info WHERE delisted=1").fetchone()[0]
    log.info("下市標記：新增 %d 檔，目前共 %d 檔下市", newly, total)
    return newly


# ---- 股票池篩選 ----
def current_disposition_ids(conn, as_of: str | None = None) -> set[str]:
    """處置期間涵蓋 as_of（預設今天）的股票代號集合。"""
    as_of = as_of or dt.date.today().isoformat()
    df = db.read_sql(
        conn,
        "SELECT DISTINCT stock_id FROM disposition WHERE period_start<=? AND period_end>=?",
        (as_of, as_of),
    )
    return set(df["stock_id"]) if not df.empty else set()


def select_universe(conn, cfg) -> list[str]:
    """依 config 的 universe 條件，從 stock_info 選出要回補的股票代號清單。

    結構性排除（市場別、代號長度、ETF）。處置股照常納入（僅在下單風控前提醒）。
    """
    u = cfg["universe"]
    df = db.read_sql(conn, """
        SELECT stock_id, stock_name, type, industry_category
        FROM stock_info WHERE COALESCE(delisted, 0) = 0
    """)
    if df.empty:
        return []

    mask = df["type"].isin(u["markets"])
    # 一般普通股代號長度（排除權證等長代號）
    mask &= df["stock_id"].str.len() == u["min_stock_id_len"]
    # 代號須為純數字（排除權證/特殊證券）
    mask &= df["stock_id"].str.isdigit()
    if not u.get("include_etf", False):
        # ETF 產業類別多為「ETF」或以 00 開頭
        mask &= ~df["stock_id"].str.startswith("00")
        mask &= df["industry_category"].fillna("") != "ETF"

    ids = sorted(df.loc[mask, "stock_id"].tolist())

    log.info("股票池篩選：%d / %d 檔符合條件", len(ids), len(df))
    return ids
