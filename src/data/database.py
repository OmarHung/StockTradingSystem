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
            delisted          INTEGER DEFAULT 0,  -- 已下市（不在券商可交易合約中）
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
    # 1 分 K 線（shioaji kbars）。ts＝該分鐘「結束」時間（台股看盤慣例：首根 09:01、
    # 收盤競價 13:30）；volume 單位＝張（指數列為市場總量）。
    # 歷史：開圖時自動回補（ensure_minute_bars）；盤中：tick 合成即時寫入（src/realtime.py）。
    "kbar_1min": """
        CREATE TABLE IF NOT EXISTS kbar_1min (
            stock_id TEXT NOT NULL,
            ts       TEXT NOT NULL,     -- 'YYYY-MM-DD HH:MM:SS'
            open     REAL,
            high     REAL,
            low      REAL,
            close    REAL,
            volume   INTEGER,           -- 張
            amount   REAL,              -- 成交金額（元）
            PRIMARY KEY (stock_id, ts)
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
    # WebUI 介面狀態（面板佈局等；跨瀏覽器/裝置一致）
    "ui_state": """
        CREATE TABLE IF NOT EXISTS ui_state (
            key   TEXT PRIMARY KEY,
            value TEXT                   -- JSON 字串
        )
    """,
    # 每日估值指標（TWSE BWIBBU_d / TPEx peQryDate，官方按日全市場）
    "valuation": """
        CREATE TABLE IF NOT EXISTS valuation (
            stock_id       TEXT NOT NULL,
            date           TEXT NOT NULL,
            per            REAL,           -- 本益比（虧損公司為 NULL）
            pbr            REAL,           -- 股價淨值比
            dividend_yield REAL,           -- 殖利率(%)
            PRIMARY KEY (stock_id, date)
        )
    """,
    # 公司行動價格調整事件（分割/反分割/減資/面額變更；跳空偵測器自動生成）
    # 台股漲跌幅 ±10% → |日報酬|>15% 且非除權息日 = 必為公司行動
    "capital_change": """
        CREATE TABLE IF NOT EXISTS capital_change (
            stock_id     TEXT NOT NULL,
            date         TEXT NOT NULL,   -- 事件日（新價格第一天）
            before_price REAL,            -- 事件前收盤
            after_price  REAL,            -- 事件日收盤
            kind         TEXT,            -- auto_split / auto_reduction（偵測器標記）
            PRIMARY KEY (stock_id, date)
        )
    """,
    # 除權息預告（TWSE TWT48U_ALL / TPEx prepost 快照；未來除權息日程）
    "dividend_forecast": """
        CREATE TABLE IF NOT EXISTS dividend_forecast (
            stock_id      TEXT NOT NULL,
            date          TEXT NOT NULL,   -- 預定除權息日
            kind          TEXT,            -- 息 / 權 / 權息
            cash_dividend REAL,            -- 現金股利（元/股）
            stock_ratio   REAL,            -- 無償配股率
            PRIMARY KEY (stock_id, date)
        )
    """,
    # 除權息結果（供還原價計算；factor = after_price / before_price）
    "dividend": """
        CREATE TABLE IF NOT EXISTS dividend (
            stock_id      TEXT NOT NULL,
            date          TEXT NOT NULL,   -- 除權息日
            before_price  REAL,            -- 除權息前收盤
            after_price   REAL,            -- 除權息參考價
            dividend      REAL,            -- 配發金額/股數
            kind          TEXT,            -- 息 / 權 / 權息
            PRIMARY KEY (stock_id, date)
        )
    """,
    # 個股新聞（FinMind TaiwanStockNews；只對進入 LLM 深度分析的個股按需抓取）
    "news": """
        CREATE TABLE IF NOT EXISTS news (
            stock_id     TEXT NOT NULL,
            date         TEXT NOT NULL,   -- 發布日期 YYYY-MM-DD
            published_at TEXT,            -- 原始時間戳（含時分秒）
            title        TEXT NOT NULL,
            source       TEXT,            -- 媒體來源
            url          TEXT,
            PRIMARY KEY (stock_id, date, title)
        )
    """,
    # 政策題材偵察每日快照（掃到的新聞標題 + LLM 總結 + 驗證後候選，供 WebUI 展示）
    "scout_log": """
        CREATE TABLE IF NOT EXISTS scout_log (
            as_of           TEXT PRIMARY KEY,
            source          TEXT,            -- rss / web
            headlines_json  TEXT,            -- [{date,title,source,url}]
            summary         TEXT,            -- LLM 題材總結
            candidates_json TEXT,            -- 驗證後候選 [{stock_id,name,theme,reason}]
            created_at      TEXT
        )
    """,
    # 處置/警示股名單（TWSE/TPEx 官方公告，含處置期間）
    "disposition": """
        CREATE TABLE IF NOT EXISTS disposition (
            stock_id     TEXT NOT NULL,
            market       TEXT,             -- twse / tpex
            name         TEXT,
            reason       TEXT,
            period_start TEXT,             -- 處置期間（ISO 日期）
            period_end   TEXT,
            fetched_at   TEXT,
            PRIMARY KEY (stock_id, period_start)
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
            note       TEXT,               -- 驗證層攔截等備註
            run_id     TEXT,               -- 同一次決策管線的批次識別（分組用）
            input_tokens       INTEGER,    -- 該次呼叫的輸入 token（未快取部分）
            output_tokens      INTEGER,
            cache_read_tokens  INTEGER,    -- 快取讀取 token（約 0.1x 價）
            cache_write_tokens INTEGER,    -- 快取寫入 token（約 1.25x 價）
            cost_usd           REAL        -- 依模型價目換算的估計成本（USD）
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
    # Friction log：被 Guard pipeline 駁回/縮減的交易紀錄（供反思層檢討風控鬆緊）
    "friction_log": """
        CREATE TABLE IF NOT EXISTS friction_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            as_of     TEXT,
            stock_id  TEXT NOT NULL,
            gate      TEXT NOT NULL,      -- 駁回的閘門（reward_risk / cooldown / ...）
            reason    TEXT,
            plan_json TEXT               -- 當時的交易計畫快照（含進出場價）
        )
    """,
    # 選股結果快照：每個基準日保留一份完整初篩結果（供 WebUI 重整/重啟後還原）。
    "screener_result": """
        CREATE TABLE IF NOT EXISTS screener_result (
            as_of      TEXT PRIMARY KEY,
            rows_json  TEXT NOT NULL,      -- 完整結果 list[dict] 的 JSON
            top_n      INTEGER,
            created_at TEXT
        )
    """,
    # 自選清單：使用者關注的個股（供 WebUI 自選面板，重整/重啟後保留）。
    "watchlist": """
        CREATE TABLE IF NOT EXISTS watchlist (
            stock_id TEXT PRIMARY KEY,
            added_at TEXT
        )
    """,
    # ---- Phase 5：模擬交易帳本 ----
    "broker_state": """
        CREATE TABLE IF NOT EXISTS broker_state (
            key   TEXT PRIMARY KEY,        -- cash / start_capital / trading_enabled
            value TEXT
        )
    """,
    "positions": """
        CREATE TABLE IF NOT EXISTS positions (
            stock_id  TEXT PRIMARY KEY,
            shares    INTEGER NOT NULL,
            avg_cost  REAL NOT NULL,
            stop_loss REAL,
            target    REAL,
            industry  TEXT,
            opened_at TEXT,
            plan_as_of TEXT               -- 對應的決策日（回溯 LLM 理由用）
        )
    """,
    "orders": """
        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_as_of TEXT NOT NULL,  -- 決策日（隔日有效）
            stock_id    TEXT NOT NULL,
            side        TEXT NOT NULL,     -- BUY / SELL
            limit_price REAL,
            shares      INTEGER NOT NULL,
            stop_loss   REAL,
            target      REAL,
            industry    TEXT,
            status      TEXT NOT NULL,     -- pending / filled / expired / cancelled
            fill_date   TEXT,
            fill_price  REAL,
            expected_fill_date TEXT        -- 掛單時算出的次交易日（跳過週末/假日）
        )
    """,
    "fills": """
        CREATE TABLE IF NOT EXISTS fills (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            date     TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            side     TEXT NOT NULL,
            shares   INTEGER NOT NULL,
            price    REAL NOT NULL,
            fee      REAL DEFAULT 0,
            tax      REAL DEFAULT 0,
            pnl      REAL,                -- 賣出時的已實現損益（含成本）
            reason   TEXT                 -- entry / stop / target / manual
        )
    """,
    "equity_history": """
        CREATE TABLE IF NOT EXISTS equity_history (
            date            TEXT PRIMARY KEY,
            cash            REAL,
            positions_value REAL,
            equity          REAL,
            taiex_close     REAL
        )
    """,
    # 台股休市日（TWSE 官方年度假日表，market_calendar 同步；週末不入表）。
    # 年度覆蓋標記記在 fetch_log（dataset='twse_holiday', stock_id=西元年）。
    "market_holiday": """
        CREATE TABLE IF NOT EXISTS market_holiday (
            date       TEXT PRIMARY KEY,   -- 休市日 YYYY-MM-DD
            name       TEXT,               -- 名稱（春節、和平紀念日…）
            fetched_at TEXT
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
    """輕量遷移：舊表補新欄位。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(fetch_log)").fetchall()]
    if cols and "first_date" not in cols:
        conn.execute("ALTER TABLE fetch_log ADD COLUMN first_date TEXT")
    # Phase 4：trade_plan 成果評估欄位
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_plan)").fetchall()]
    if cols:
        for col, ddl in (("outcome", "TEXT"), ("outcome_return", "REAL"), ("evaluated_at", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE trade_plan ADD COLUMN {col} {ddl}")
    # 大腦活動分組：brain_log 批次識別
    cols = [r[1] for r in conn.execute("PRAGMA table_info(brain_log)").fetchall()]
    if cols and "run_id" not in cols:
        conn.execute("ALTER TABLE brain_log ADD COLUMN run_id TEXT")
    # LLM 用量/成本追蹤（供 WebUI 顯示 Claude credit 花費與剩餘估計）
    if cols:
        for col, ddl in (("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"),
                         ("cache_read_tokens", "INTEGER"), ("cache_write_tokens", "INTEGER"),
                         ("cost_usd", "REAL")):
            if col not in cols:
                conn.execute(f"ALTER TABLE brain_log ADD COLUMN {col} {ddl}")
    # 下市標記：stock_info（FinMind 清單含歷史下市代號，需旗標過濾）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_info)").fetchall()]
    if cols and "delisted" not in cols:
        conn.execute("ALTER TABLE stock_info ADD COLUMN delisted INTEGER DEFAULT 0")
    # 委託單預計撮合日（依交易日曆跳過週末/假日）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if cols and "expected_fill_date" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN expected_fill_date TEXT")


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # timeout：寫入鎖被背景任務占用時最多等 30 秒，避免動輒 database is locked
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")   # 較好的併發讀寫
    conn.execute("PRAGMA busy_timeout=30000")
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
