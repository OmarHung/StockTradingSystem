"""SQLite 資料庫：schema 定義、連線、upsert 與查詢輔助。

各表主鍵設計皆為 (stock_id, date)（月營收為 (stock_id, revenue_year, revenue_month)），
搭配 INSERT OR REPLACE 達成冪等回補——重跑不會產生重複列。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd

# ---- Schema ----
# 每張表對應一個 FinMind dataset，欄位命名貼近原始回傳以降低轉換心智負擔。
SCHEMA: dict[str, str] = {
    # 股票基本資料（清單）
    "stock_info": """
        CREATE TABLE IF NOT EXISTS stock_info (
            stock_id          TEXT NOT NULL,
            stock_name        TEXT,
            industry_category TEXT,
            type              TEXT,          -- twse / tpex
            date              TEXT,          -- 資料日期
            PRIMARY KEY (stock_id)
        )
    """,
    # 日 K 線
    "price_daily": """
        CREATE TABLE IF NOT EXISTS price_daily (
            stock_id         TEXT NOT NULL,
            date             TEXT NOT NULL,
            open             REAL,
            high             REAL,          -- FinMind 原欄位 max
            low              REAL,          -- FinMind 原欄位 min
            close            REAL,
            volume           INTEGER,       -- Trading_Volume（股數）
            trading_money    INTEGER,       -- Trading_money（元）
            trading_turnover INTEGER,       -- 成交筆數
            spread           REAL,          -- 漲跌價差
            PRIMARY KEY (stock_id, date)
        )
    """,
    # 三大法人買賣超（每列一個法人別，用 name 區分）
    "institutional": """
        CREATE TABLE IF NOT EXISTS institutional (
            stock_id TEXT NOT NULL,
            date     TEXT NOT NULL,
            name     TEXT NOT NULL,         -- Foreign_Investor / Investment_Trust / Dealer_self ...
            buy      INTEGER,
            sell     INTEGER,
            PRIMARY KEY (stock_id, date, name)
        )
    """,
    # 融資融券
    "margin": """
        CREATE TABLE IF NOT EXISTS margin (
            stock_id              TEXT NOT NULL,
            date                  TEXT NOT NULL,
            margin_purchase_buy   INTEGER,
            margin_purchase_sell  INTEGER,
            margin_purchase_balance INTEGER,
            short_sale_buy        INTEGER,
            short_sale_sell       INTEGER,
            short_sale_balance    INTEGER,
            PRIMARY KEY (stock_id, date)
        )
    """,
    # 月營收
    "month_revenue": """
        CREATE TABLE IF NOT EXISTS month_revenue (
            stock_id      TEXT NOT NULL,
            date          TEXT,             -- 公告日期
            revenue_year  INTEGER NOT NULL,
            revenue_month INTEGER NOT NULL,
            revenue       INTEGER,
            PRIMARY KEY (stock_id, revenue_year, revenue_month)
        )
    """,
    # LLM 呼叫記錄（供 WebUI「大腦活動」頁）
    "brain_log": """
        CREATE TABLE IF NOT EXISTS brain_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,       -- 呼叫時間
            as_of      TEXT,               -- 分析基準日
            stock_id   TEXT,
            agent      TEXT NOT NULL,       -- technical / chips / fundamental / trader / validator
            model      TEXT,               -- 實際使用模型（含 fallback 後）
            prompt     TEXT,               -- 送出的 user prompt
            response   TEXT,               -- 原始回應（JSON 字串）
            note       TEXT                -- 驗證層攔截等備註
        )
    """,
    # 交易計畫（交易員 Agent 產出，供選股報告與後續執行）
    "trade_plan": """
        CREATE TABLE IF NOT EXISTS trade_plan (
            as_of        TEXT NOT NULL,
            stock_id     TEXT NOT NULL,
            action       TEXT,
            action_score REAL,
            confidence   REAL,
            entry_low    REAL,
            entry_high   REAL,
            stop_loss    REAL,
            target_price REAL,
            reward_risk  REAL,
            rationale    TEXT,
            plan_json    TEXT,             -- 完整計畫 + 各分析師報告（JSON）
            created_at   TEXT,
            PRIMARY KEY (as_of, stock_id)
        )
    """,
    # 回補進度紀錄：記錄每檔每類資料「已補範圍」(first_date~last_date)，
    # 供「最新優先」兩趟回補判斷缺口。
    "fetch_log": """
        CREATE TABLE IF NOT EXISTS fetch_log (
            dataset    TEXT NOT NULL,
            stock_id   TEXT NOT NULL,
            first_date TEXT,
            last_date  TEXT,
            updated_at TEXT,
            PRIMARY KEY (dataset, stock_id)
        )
    """,
}


def _migrate(conn: sqlite3.Connection) -> None:
    """輕量遷移：舊 fetch_log 補上 first_date 欄位。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(fetch_log)").fetchall()]
    if cols and "first_date" not in cols:
        conn.execute("ALTER TABLE fetch_log ADD COLUMN first_date TEXT")


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")   # 較好的併發讀寫
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """建立所有資料表（若不存在）並執行輕量遷移。"""
    with get_connection(db_path) as conn:
        for ddl in SCHEMA.values():
            conn.execute(ddl)
        _migrate(conn)
        conn.commit()


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_dataframe(
    conn: sqlite3.Connection, table: str, df: pd.DataFrame
) -> int:
    """以 INSERT OR REPLACE 將 DataFrame 寫入 table。

    df 欄位須為 table 欄位的子集；回傳寫入列數。冪等——同主鍵重跑會覆蓋。
    """
    if df is None or df.empty:
        return 0

    # 只保留資料表中存在的欄位，避免多餘欄位報錯
    table_cols = _table_columns(conn, table)
    cols = [c for c in df.columns if c in table_cols]
    if not cols:
        raise ValueError(f"DataFrame 沒有任何欄位對應資料表 {table}（table 欄位：{table_cols}）")

    df = df[cols].where(pd.notnull(df[cols]), None)
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(cols)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
    conn.executemany(sql, df.itertuples(index=False, name=None))
    return len(df)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def read_sql(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def get_last_date(conn: sqlite3.Connection, dataset: str, stock_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_date FROM fetch_log WHERE dataset=? AND stock_id=?",
        (dataset, stock_id),
    ).fetchone()
    return row[0] if row else None


def get_range(conn: sqlite3.Connection, dataset: str, stock_id: str) -> tuple[str | None, str | None]:
    """回傳該檔該 dataset 已補的 (first_date, last_date)；無紀錄則 (None, None)。"""
    row = conn.execute(
        "SELECT first_date, last_date FROM fetch_log WHERE dataset=? AND stock_id=?",
        (dataset, stock_id),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def merge_range(
    conn: sqlite3.Connection, dataset: str, stock_id: str,
    new_first: str, new_last: str, updated_at: str,
) -> None:
    """把新抓到的 [new_first, new_last] 併入既有已補範圍（取聯集端點）。"""
    old_first, old_last = get_range(conn, dataset, stock_id)
    first = min(x for x in (old_first, new_first) if x)
    last = max(x for x in (old_last, new_last) if x)
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log (dataset, stock_id, first_date, last_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (dataset, stock_id, first, last, updated_at),
    )
