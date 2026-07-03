"""資料查詢 API：供 Screener、回測、WebUI 讀取已入庫資料。

刻意與寫入路徑分離，讓上層只依賴穩定的查詢介面，不碰 SQL 細節。
"""
from __future__ import annotations

import pandas as pd

from src.config import get_settings
from src.data import database as db


def _db_path():
    return get_settings().db_path


def get_price(stock_id: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """取單檔日 K，依日期排序。"""
    sql = "SELECT * FROM price_daily WHERE stock_id=?"
    params: list = [stock_id]
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    sql += " ORDER BY date"
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, sql, tuple(params))


def get_institutional(stock_id: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """取單檔三大法人買賣超（long 格式，每列一個法人別）。"""
    sql = "SELECT * FROM institutional WHERE stock_id=?"
    params: list = [stock_id]
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    sql += " ORDER BY date, name"
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, sql, tuple(params))


def get_month_revenue(stock_id: str) -> pd.DataFrame:
    with db.connect(_db_path()) as conn:
        return db.read_sql(
            conn,
            "SELECT * FROM month_revenue WHERE stock_id=? ORDER BY revenue_year, revenue_month",
            (stock_id,),
        )


def get_prices_bulk(stock_ids: list[str], start: str, end: str) -> pd.DataFrame:
    """一次取多檔日 K（供 Screener 橫斷面計算）。回傳含所有股票的長格式。"""
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    sql = (
        f"SELECT * FROM price_daily WHERE stock_id IN ({placeholders}) "
        f"AND date>=? AND date<=? ORDER BY stock_id, date"
    )
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, sql, tuple(stock_ids) + (start, end))


def get_institutional_bulk(stock_ids: list[str], start: str, end: str) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    sql = (
        f"SELECT * FROM institutional WHERE stock_id IN ({placeholders}) "
        f"AND date>=? AND date<=? ORDER BY stock_id, date"
    )
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, sql, tuple(stock_ids) + (start, end))


def get_revenue_bulk(stock_ids: list[str], as_of: str) -> pd.DataFrame:
    """取多檔在 as_of（含）之前已公告的月營收（避免前視偏差）。"""
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    sql = (
        f"SELECT * FROM month_revenue WHERE stock_id IN ({placeholders}) "
        f"AND date<=? ORDER BY stock_id, revenue_year, revenue_month"
    )
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, sql, tuple(stock_ids) + (as_of,))


def all_stock_ids() -> list[str]:
    with db.connect(_db_path()) as conn:
        df = db.read_sql(conn, "SELECT DISTINCT stock_id FROM price_daily ORDER BY stock_id")
    return df["stock_id"].tolist()


def list_stocks() -> pd.DataFrame:
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, "SELECT * FROM stock_info ORDER BY stock_id")


def brain_log(limit: int = 100, as_of: str | None = None) -> pd.DataFrame:
    """讀取最近的 LLM 呼叫/驗證記錄（供大腦活動頁）。"""
    sql = "SELECT id, ts, as_of, stock_id, agent, model, prompt, response, note FROM brain_log"
    params: tuple = ()
    if as_of:
        sql += " WHERE as_of=?"
        params = (as_of,)
    sql += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, sql, params)


def data_status() -> pd.DataFrame:
    """各資料表的覆蓋概況（供 WebUI 資料狀態頁）。"""
    rows = []
    with db.connect(_db_path()) as conn:
        for table in ("price_daily", "institutional", "margin", "month_revenue"):
            r = db.read_sql(
                conn,
                f"SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_id) AS stocks, "
                f"MIN(date) AS min_date, MAX(date) AS max_date FROM {table}",
            )
            r.insert(0, "table", table)
            rows.append(r)
    return pd.concat(rows, ignore_index=True)
