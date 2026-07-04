"""資料查詢 API：供 Screener、回測、WebUI 讀取已入庫資料。

刻意與寫入路徑分離，讓上層只依賴穩定的查詢介面，不碰 SQL 細節。
"""
from __future__ import annotations

import pandas as pd

from src.config import get_settings
from src.data import database as db


def _db_path():
    return get_settings().db_path


def get_price(
    stock_id: str,
    start: str | None = None,
    end: str | None = None,
    adjusted: bool = False,
) -> pd.DataFrame:
    """取單檔日 K，依日期排序。

    adjusted=True 回傳「還原價」（backward adjustment）：以除權息事件的
    after_price/before_price 為調整係數，事件日之前的價格乘上累積係數，
    使歷史價格與現價可比（動能/均線/回測必用，否則除權息跳空會污染訊號）。
    量能不調整。
    """
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
        df = _clean_price(db.read_sql(conn, sql, tuple(params)))
        if not adjusted or df.empty:
            return df
        div = db.read_sql(
            conn,
            "SELECT date, before_price, after_price FROM dividend "
            "WHERE stock_id=? AND before_price>0 AND after_price>0 ORDER BY date",
            (stock_id,),
        )
    return _apply_adjustment(df, div)


def _clean_price(df: pd.DataFrame) -> pd.DataFrame:
    """查詢時清洗（MT5 式：原始照存、讀取修復）：

    - 剔除全零價格列（該日無成交，close=0 會毒害均線/動能/回測）
    - open 夾回 [low, high]（來源資料偶有 open 落在區間外的錯誤）
    """
    if df.empty:
        return df
    df = df[(df["close"] > 0) & (df["high"] > 0)].copy()
    if df.empty:
        return df
    df["open"] = df["open"].clip(lower=df["low"], upper=df["high"])
    # open 為 0（無效）時以當日 low 補
    df.loc[df["open"] <= 0, "open"] = df["low"]
    return df.reset_index(drop=True)


def _apply_adjustment(df: pd.DataFrame, div: pd.DataFrame) -> pd.DataFrame:
    """把除權息調整係數套用到價格欄位（backward：除權息日前的價格 × 係數）。"""
    if div.empty:
        return df
    df = df.copy()
    factor = pd.Series(1.0, index=df.index)
    for _, ev in div.iterrows():
        f = float(ev["after_price"]) / float(ev["before_price"])
        # 除權息日(含)之後不變，之前全部乘上係數
        factor[df["date"] < ev["date"]] *= f
    for col in ("open", "high", "low", "close"):
        df[col] = (df[col] * factor).round(2)
    return df


# ---- 多時間框架（MT5 式：只存日線，週/月即時聚合）----
def resample_price(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """把日 K 聚合成週 K（'W'）或月 K（'M'）。輸入需含 date/open/high/low/close/volume。"""
    if df.empty:
        return df
    x = df.copy()
    x["date"] = pd.to_datetime(x["date"])
    rule = {"W": "W-FRI", "M": "ME"}.get(freq, freq)
    g = x.set_index("date").resample(rule)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    }).dropna(subset=["open"])
    out.index = out.index.strftime("%Y-%m-%d")
    return out.reset_index().rename(columns={"date": "date", "index": "date"})


# ---- 交易日曆（以加權指數 TAIEX 的交易日為準）----
def trading_calendar(start: str | None = None, end: str | None = None) -> list[str]:
    sql = "SELECT date FROM price_daily WHERE stock_id='TAIEX'"
    params: list = []
    if start:
        sql += " AND date>=?"; params.append(start)
    if end:
        sql += " AND date<=?"; params.append(end)
    sql += " ORDER BY date"
    with db.connect(_db_path()) as conn:
        df = db.read_sql(conn, sql, tuple(params))
    return df["date"].tolist()


# ---- 資料品質檢查（缺日偵測 + OHLC 異常）----
def quality_check(sample_limit: int = 20) -> dict:
    """對照交易日曆偵測缺日、檢查 OHLC 異常。回傳摘要 + 問題樣本。

    缺日以「該檔自身資料範圍內」對照 TAIEX 交易日（上市前/下市後不算缺）。
    """
    cal = trading_calendar()
    result: dict = {"calendar_days": len(cal), "checked_stocks": 0,
                    "stocks_with_gaps": 0, "total_missing_days": 0,
                    "ohlc_anomalies": 0, "gap_samples": [], "anomaly_samples": []}
    if not cal:
        result["error"] = "無 TAIEX 交易日曆，請先回補（指數已納入預設回補目標）"
        return result
    cal_set = set(cal)

    with db.connect(_db_path()) as conn:
        stocks = db.read_sql(
            conn, "SELECT stock_id, MIN(date) AS lo, MAX(date) AS hi, COUNT(*) AS n "
                  "FROM price_daily WHERE stock_id NOT IN ('TAIEX','TPEx') GROUP BY stock_id")
        # 全零價格列（無成交日）：查詢層已自動剔除，這裡僅供資訊
        zero_rows = db.read_sql(
            conn, "SELECT COUNT(*) AS c FROM price_daily WHERE close<=0 OR high<=0")
        # 結構異常（高低倒置、收盤出界）：查詢層會夾 open，但這類仍值得人工看
        anomalies = db.read_sql(
            conn,
            "SELECT stock_id, date, open, high, low, close FROM price_daily "
            "WHERE close>0 AND high>0 AND (high < low OR close > high OR close < low) "
            "LIMIT 500",
        )
    result["zero_price_rows"] = int(zero_rows.iloc[0]["c"])

    for r in stocks.itertuples():
        expected = [d for d in cal if r.lo <= d <= r.hi]
        missing = len(expected) - r.n
        result["checked_stocks"] += 1
        if missing > 0:
            result["stocks_with_gaps"] += 1
            result["total_missing_days"] += missing
            if len(result["gap_samples"]) < sample_limit:
                result["gap_samples"].append(
                    {"stock_id": r.stock_id, "range": f"{r.lo}~{r.hi}",
                     "expected": len(expected), "actual": r.n, "missing": missing})

    result["ohlc_anomalies"] = len(anomalies)
    result["anomaly_samples"] = anomalies.head(sample_limit).to_dict(orient="records")
    return result


def get_dividends(stock_id: str) -> pd.DataFrame:
    with db.connect(_db_path()) as conn:
        return db.read_sql(
            conn, "SELECT * FROM dividend WHERE stock_id=? ORDER BY date", (stock_id,))


def list_disposition(active_on: str | None = None) -> pd.DataFrame:
    """處置股名單；active_on 給日期則只回傳該日仍在處置期間者。"""
    with db.connect(_db_path()) as conn:
        if active_on:
            return db.read_sql(
                conn, "SELECT * FROM disposition WHERE period_start<=? AND period_end>=? "
                      "ORDER BY period_end DESC", (active_on, active_on))
        return db.read_sql(conn, "SELECT * FROM disposition ORDER BY period_end DESC")


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


def get_prices_bulk(
    stock_ids: list[str], start: str, end: str, adjusted: bool = False
) -> pd.DataFrame:
    """一次取多檔日 K（供 Screener 橫斷面計算）。回傳含所有股票的長格式。

    adjusted=True 逐檔套用除權息還原（同 get_price）。
    """
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    sql = (
        f"SELECT * FROM price_daily WHERE stock_id IN ({placeholders}) "
        f"AND date>=? AND date<=? ORDER BY stock_id, date"
    )
    with db.connect(_db_path()) as conn:
        df = _clean_price(db.read_sql(conn, sql, tuple(stock_ids) + (start, end)))
        if not adjusted or df.empty:
            return df
        div_all = db.read_sql(
            conn,
            f"SELECT stock_id, date, before_price, after_price FROM dividend "
            f"WHERE stock_id IN ({placeholders}) AND before_price>0 AND after_price>0 "
            f"ORDER BY stock_id, date",
            tuple(stock_ids),
        )
    if div_all.empty:
        return df
    parts = []
    div_g = {sid: g for sid, g in div_all.groupby("stock_id")}
    for sid, g in df.groupby("stock_id"):
        parts.append(_apply_adjustment(g, div_g.get(sid, pd.DataFrame())))
    return pd.concat(parts, ignore_index=True)


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
        for table in ("price_daily", "institutional", "margin", "month_revenue", "dividend"):
            r = db.read_sql(
                conn,
                f"SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_id) AS stocks, "
                f"MIN(date) AS min_date, MAX(date) AS max_date FROM {table}",
            )
            r.insert(0, "table", table)
            rows.append(r)
    return pd.concat(rows, ignore_index=True)
