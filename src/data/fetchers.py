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


# ---- 日 K ----
_PRICE_MAP = {
    "max": "high",
    "min": "low",
    "Trading_Volume": "volume",
    "Trading_money": "trading_money",
    "Trading_turnover": "trading_turnover",
}


def fetch_price(client: FinMindClient, conn, stock_id: str, start: str, end: str) -> int:
    raw = client.get_dataset("TaiwanStockPrice", data_id=stock_id, start_date=start, end_date=end)
    if raw.empty:
        return 0
    df = raw.rename(columns=_PRICE_MAP)
    n = db.upsert_dataframe(conn, "price_daily", df)
    _update_log(conn, "price_daily", stock_id, df["date"])
    return n


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


def _update_log(conn, dataset: str, stock_id: str, dates: pd.Series) -> None:
    if dates.empty:
        return
    db.merge_range(
        conn, dataset, stock_id,
        str(dates.min()), str(dates.max()),
        dt.datetime.now().isoformat(timespec="seconds"),
    )


# ---- 股票池篩選 ----
def select_universe(conn, cfg) -> list[str]:
    """依 config 的 universe 條件，從 stock_info 選出要回補的股票代號清單。

    Phase 0 先做結構性排除（市場別、代號長度、ETF）；處置股/全額交割股需事件資料，
    於後續階段以獨立 dataset 強化（此處旗標保留但尚未生效）。
    """
    u = cfg["universe"]
    df = db.read_sql(conn, "SELECT stock_id, stock_name, type, industry_category FROM stock_info")
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
