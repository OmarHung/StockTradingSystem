"""資料查詢 API：供 Screener、回測、WebUI 讀取已入庫資料。

刻意與寫入路徑分離，讓上層只依賴穩定的查詢介面，不碰 SQL 細節。
"""
from __future__ import annotations

import datetime as dt
import json

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

    缺日分兩類（關鍵區分，避免誤報）：
    - 無成交/停牌日（正常）：該日全市場已成功抓過（sj_daily 有標記或該日
      多數股票有資料），該股就是沒成交——冷門股/停牌常見，不是資料洞。
    - 真缺日：該日不在任何已抓來源覆蓋內 → 需要回補。
    """
    cal = trading_calendar()
    result: dict = {"calendar_days": len(cal), "checked_stocks": 0,
                    "stocks_with_gaps": 0, "total_missing_days": 0,
                    "no_trade_days": 0,
                    "ohlc_anomalies": 0, "gap_samples": [], "anomaly_samples": []}
    if not cal:
        result["error"] = "無 TAIEX 交易日曆，請先回補（指數已納入預設回補目標）"
        return result

    with db.connect(_db_path()) as conn:
        stocks = db.read_sql(
            conn, "SELECT stock_id, MIN(date) AS lo, MAX(date) AS hi, COUNT(*) AS n "
                  "FROM price_daily WHERE stock_id NOT IN ('TAIEX','TPEx') GROUP BY stock_id")
        per_stock_dates = db.read_sql(
            conn, "SELECT stock_id, date FROM price_daily WHERE stock_id NOT IN ('TAIEX','TPEx')")
        # 「全市場已覆蓋」的日子：shioaji 按日標記 ∪ 當日有半數以上股票有資料
        sj_done = {r[0] for r in conn.execute(
            "SELECT stock_id FROM fetch_log WHERE dataset='sj_daily'").fetchall()}
        day_counts = db.read_sql(
            conn, "SELECT date, COUNT(DISTINCT stock_id) AS c FROM price_daily "
                  "WHERE stock_id NOT IN ('TAIEX','TPEx') GROUP BY date")
        median_c = day_counts["c"].median() if not day_counts.empty else 0
        busy_days = set(day_counts[day_counts["c"] >= median_c * 0.5]["date"])
        covered = sj_done | busy_days
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

    have_map = {sid: set(g["date"]) for sid, g in per_stock_dates.groupby("stock_id")}
    for r in stocks.itertuples():
        result["checked_stocks"] += 1
        have = have_map.get(r.stock_id, set())
        missing = [d for d in cal if r.lo <= d <= r.hi and d not in have]
        if not missing:
            continue
        real = [d for d in missing if d not in covered]     # 真缺日（來源沒抓過）
        result["no_trade_days"] += len(missing) - len(real)  # 無成交/停牌（正常）
        if real:
            result["stocks_with_gaps"] += 1
            result["total_missing_days"] += len(real)
            if len(result["gap_samples"]) < sample_limit:
                result["gap_samples"].append(
                    {"stock_id": r.stock_id, "range": f"{r.lo}~{r.hi}",
                     "missing": len(real), "days": real[:5]})

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


def get_dividend_forecast(stock_id: str, after: str | None = None) -> pd.DataFrame:
    """除權息預告（預定日 > after 的未來日程，日期升冪）。"""
    sql = "SELECT * FROM dividend_forecast WHERE stock_id=?"
    params: list = [stock_id]
    if after:
        sql += " AND date > ?"
        params.append(after)
    sql += " ORDER BY date"
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, sql, tuple(params))


def get_valuation(stock_id: str) -> pd.DataFrame:
    """每日估值（本益比/股價淨值比/殖利率%），日期升冪。"""
    with db.connect(_db_path()) as conn:
        return db.read_sql(
            conn,
            "SELECT * FROM valuation WHERE stock_id=? ORDER BY date",
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
    """已有價格資料的個股清單（排除大盤指數，避免混入選股池）。"""
    with db.connect(_db_path()) as conn:
        df = db.read_sql(
            conn,
            "SELECT DISTINCT stock_id FROM price_daily "
            "WHERE stock_id NOT IN ('TAIEX','TPEx') ORDER BY stock_id",
        )
    return df["stock_id"].tolist()


def list_stocks() -> pd.DataFrame:
    with db.connect(_db_path()) as conn:
        return db.read_sql(conn, "SELECT * FROM stock_info ORDER BY stock_id")


def stocks_overview() -> list[dict]:
    """全市場股票總覽（含尚未下載/處置股），供 WebUI 股票瀏覽器。

    每檔附：市場、產業、資料下載狀態（價格天數/最後日期）、是否處置中。
    """
    import datetime as dt
    today = dt.date.today().isoformat()
    with db.connect(_db_path()) as conn:
        df = db.read_sql(conn, """
            SELECT s.stock_id, s.stock_name, s.industry_category, s.type AS market,
                   COALESCE(p.n, 0) AS price_days, p.last_date AS price_last
            FROM stock_info s
            LEFT JOIN (SELECT stock_id, COUNT(*) AS n, MAX(date) AS last_date
                       FROM price_daily GROUP BY stock_id) p
              ON p.stock_id = s.stock_id
            ORDER BY s.stock_id
        """)
        disp = {r[0] for r in conn.execute(
            "SELECT DISTINCT stock_id FROM disposition WHERE period_start<=? AND period_end>=?",
            (today, today))}
    out = []
    for r in df.itertuples():
        out.append({
            "stock_id": r.stock_id, "name": r.stock_name or "",
            "industry": r.industry_category or "（未分類）",
            "market": r.market or "",
            "price_days": int(r.price_days),
            # LEFT JOIN 無資料時是 NaN，轉 None 才是合法 JSON
            "price_last": r.price_last if isinstance(r.price_last, str) else None,
            "downloaded": int(r.price_days) > 0,
            "disposition": r.stock_id in disp,
        })
    return out


def stock_detail(stock_id: str) -> dict:
    """單一股票的資料總覽：基本資料 + 各資料集覆蓋 + 最新關鍵數據。"""
    with db.connect(_db_path()) as conn:
        info = db.read_sql(conn, "SELECT * FROM stock_info WHERE stock_id=?", (stock_id,))
        detail: dict = {
            "stock_id": stock_id,
            "name": info.iloc[0]["stock_name"] if not info.empty else "",
            "industry": (info.iloc[0]["industry_category"] if not info.empty else "") or "（未分類）",
            "market": info.iloc[0]["type"] if not info.empty else "",
        }
        # 各資料集覆蓋範圍
        cov = {}
        for table, label in (("price_daily", "股價日K"), ("institutional", "三大法人"),
                             ("margin", "融資融券"), ("dividend", "除權息"),
                             ("valuation", "估值(PER/PBR/殖利率)")):
            r = db.read_sql(conn, f"SELECT COUNT(*) n, MIN(date) lo, MAX(date) hi "
                                  f"FROM {table} WHERE stock_id=?", (stock_id,)).iloc[0]
            cov[table] = {"label": label, "rows": int(r["n"]), "from": r["lo"], "to": r["hi"]}
        rev = db.read_sql(conn, "SELECT COUNT(*) n, MAX(revenue_year*100+revenue_month) ym "
                                "FROM month_revenue WHERE stock_id=?", (stock_id,)).iloc[0]
        cov["month_revenue"] = {"label": "月營收", "rows": int(rev["n"]),
                                "from": None, "to": (str(rev["ym"])[:4] + "-" + str(rev["ym"])[4:]) if rev["ym"] else None}
        detail["coverage"] = cov
        # 處置狀態
        disp = db.read_sql(conn, "SELECT reason, period_start, period_end FROM disposition "
                                 "WHERE stock_id=? ORDER BY period_end DESC LIMIT 1", (stock_id,))
        detail["disposition"] = disp.to_dict(orient="records")[0] if not disp.empty else None

    # 最新關鍵數據（還原價、法人5日、融資餘額、營收YoY）——資料不足時各自為空
    px = get_price(stock_id, adjusted=True)
    if not px.empty:
        last, prev = px.iloc[-1], (px.iloc[-2] if len(px) > 1 else px.iloc[-1])
        detail["quote"] = {
            "date": last["date"], "close": float(last["close"]),
            "change_pct": round((float(last["close"]) / float(prev["close"]) - 1) * 100, 2) if float(prev["close"]) else 0,
            "volume": int(last["volume"] or 0),
        }
    try:
        from src.agents import features as F
        detail["chips"] = F.chips_features(stock_id, px.iloc[-1]["date"]) if not px.empty else {}
        detail["fundamental"] = F.fundamental_features(
            stock_id, px.iloc[-1]["date"] if not px.empty else "2099-12-31")
    except Exception:  # noqa: BLE001
        pass
    with db.connect(_db_path()) as conn:
        m = db.read_sql(conn, "SELECT date, margin_purchase_balance, short_sale_balance "
                              "FROM margin WHERE stock_id=? ORDER BY date DESC LIMIT 1", (stock_id,))
        detail["margin"] = m.to_dict(orient="records")[0] if not m.empty else None
    return detail


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


# 資料表 → 使用者看得懂的說明
_DATASET_META = {
    "price_daily":  {"label": "股價日K線",     "desc": "開高低收與成交量（選股/回測/K線圖）"},
    "institutional": {"label": "三大法人買賣超", "desc": "外資/投信/自營商動向（籌碼分析）"},
    "margin":        {"label": "融資融券",       "desc": "散戶槓桿與軋空訊號（籌碼分析）"},
    "month_revenue": {"label": "月營收",         "desc": "每月10日前公告（基本面分析）"},
    "dividend":      {"label": "除權息",         "desc": "還原價計算必需，缺了動能會失真"},
    "valuation":     {"label": "估值指標",       "desc": "本益比/股價淨值比/殖利率（基本面分析）"},
}
# 月營收是月頻資料，新鮮度用「天」衡量而非交易日
_MONTHLY = {"month_revenue"}


def data_status() -> dict:
    """資料健康報告（供 WebUI 資料狀態視窗）。

    每個資料集回：中文名稱、覆蓋率（幾檔/股票池）、最新日期、落後天數、狀態燈。
    附整體結論 summary（該不該回補、缺什麼）。
    """
    import datetime as dt

    with db.connect(_db_path()) as conn:
        # 股票池大小（上市+上櫃普通股，同回補目標口徑）
        uni = db.read_sql(
            conn,
            "SELECT COUNT(*) AS c FROM stock_info "
            "WHERE type IN ('twse','tpex') AND length(stock_id)=4 "
            "AND stock_id GLOB '[0-9][0-9][0-9][0-9]'",
        )
        universe = int(uni.iloc[0]["c"]) if not uni.empty else 0

        # 最新交易日（TAIEX 為準；沒有就用今天）
        cal = db.read_sql(conn, "SELECT MAX(date) AS d FROM price_daily WHERE stock_id='TAIEX'")
        latest_trading = cal.iloc[0]["d"] if not cal.empty and cal.iloc[0]["d"] else None

        datasets = []
        for table, meta in _DATASET_META.items():
            # 覆蓋率只計股票池成員（官方全市場源會多抓 ETF/特殊證券，避免 >100%）
            r = db.read_sql(
                conn,
                f"SELECT COUNT(*) AS n, COUNT(DISTINCT t.stock_id) AS stocks, "
                f"MIN(t.date) AS lo, MAX(t.date) AS hi FROM {table} t "
                f"JOIN stock_info s ON s.stock_id = t.stock_id "
                f"WHERE s.type IN ('twse','tpex') AND length(t.stock_id)=4 "
                f"AND t.stock_id GLOB '[0-9][0-9][0-9][0-9]'",
            ).iloc[0]
            stocks_n = int(r["stocks"])
            coverage = round(stocks_n / universe * 100) if universe else 0

            # 新鮮度：落後最新交易日幾天（月營收放寬到 40 天內算正常）
            lag_days = None
            if r["hi"]:
                ref = latest_trading or dt.date.today().isoformat()
                lag_days = (dt.date.fromisoformat(ref) - dt.date.fromisoformat(r["hi"])).days

            if stocks_n == 0:
                status, hint = "missing", "尚未回補"
            elif table in _MONTHLY:
                status = "ok" if (lag_days is not None and lag_days <= 40) else "stale"
                hint = "最新" if status == "ok" else f"落後 {lag_days} 天"
            elif lag_days is not None and lag_days <= 1:
                status, hint = "ok", "最新"
            elif lag_days is not None and lag_days <= 7:
                status, hint = "stale", f"落後 {lag_days} 天"
            else:
                status, hint = "stale", f"落後 {lag_days} 天" if lag_days is not None else "未知"
            # 覆蓋率低於 8 成一律降級提醒
            if stocks_n > 0 and coverage < 80:
                status = "partial" if status == "ok" else status
                hint += f"，僅覆蓋 {coverage}% 股票池"

            datasets.append({
                "table": table, "label": meta["label"], "desc": meta["desc"],
                "stocks": stocks_n, "universe": universe, "coverage_pct": coverage,
                "first_date": r["lo"], "last_date": r["hi"],
                "lag_days": lag_days, "status": status, "hint": hint,
            })

    # 整體結論
    problems = []
    for d in datasets:
        if d["status"] == "missing":
            problems.append(f"「{d['label']}」尚未回補")
        elif d["coverage_pct"] < 80 and d["stocks"] > 0:
            problems.append(f"「{d['label']}」只有 {d['stocks']}/{d['universe']} 檔")
        elif d["status"] == "stale":
            problems.append(f"「{d['label']}」{d['hint']}")
    if problems:
        summary = {"level": "warn", "text": "建議執行回補：" + "；".join(problems[:4])}
    else:
        summary = {"level": "ok", "text": "資料完整且新鮮，無需回補。"}

    return {"latest_trading_day": latest_trading, "universe": universe,
            "datasets": datasets, "summary": summary}


# ---------- 選股結果快照（重整/重啟後還原）----------
_SCREENER_DDL = db.SCHEMA["screener_result"]


def save_screener_result(as_of: str, rows: list[dict], top_n: int | None = None) -> None:
    """保存某基準日的選股結果（同日期覆蓋）。"""
    with db.connect(_db_path()) as conn:
        conn.execute(_SCREENER_DDL)  # 防禦性建表：舊 DB 未 init 也能用
        conn.execute(
            "INSERT OR REPLACE INTO screener_result (as_of, rows_json, top_n, created_at) "
            "VALUES (?,?,?,?)",
            (as_of, json.dumps(rows, ensure_ascii=False), top_n,
             dt.datetime.now().isoformat(timespec="seconds")),
        )


def load_screener_result(as_of: str) -> dict | None:
    """讀取某基準日已存的選股結果；無則回 None。"""
    with db.connect(_db_path()) as conn:
        conn.execute(_SCREENER_DDL)
        row = conn.execute(
            "SELECT rows_json, top_n, created_at FROM screener_result WHERE as_of=?",
            (as_of,),
        ).fetchone()
    if not row:
        return None
    return {"as_of": as_of, "rows": json.loads(row[0]),
            "top_n": row[1], "created_at": row[2]}


def list_screener_dates() -> list[dict]:
    """已保存選股結果的日期清單（新到舊），供歷史選單。"""
    with db.connect(_db_path()) as conn:
        conn.execute(_SCREENER_DDL)
        rows = conn.execute(
            "SELECT as_of, created_at FROM screener_result ORDER BY as_of DESC"
        ).fetchall()
    return [{"as_of": r[0], "created_at": r[1]} for r in rows]


# ---------- 自選清單（重整/重啟後保留）----------
_WATCHLIST_DDL = db.SCHEMA["watchlist"]
_WATCHLIST_SEED = ["1101", "1102", "1216", "1301", "2330", "2317", "0050"]


def list_watchlist() -> list[str]:
    """自選股代碼清單（依加入時間排序）。首次為空時以預設清單種子。"""
    with db.connect(_db_path()) as conn:
        conn.execute(_WATCHLIST_DDL)
        rows = conn.execute(
            "SELECT stock_id FROM watchlist ORDER BY added_at, stock_id"
        ).fetchall()
        if not rows:  # 一次性種子，保留原本預設清單體驗
            now = dt.datetime.now().isoformat(timespec="seconds")
            conn.executemany(
                "INSERT OR IGNORE INTO watchlist (stock_id, added_at) VALUES (?,?)",
                [(sid, now) for sid in _WATCHLIST_SEED],
            )
            return list(_WATCHLIST_SEED)
    return [r[0] for r in rows]


def add_watchlist(stock_id: str) -> list[str]:
    """加入自選（冪等），回傳更新後清單。"""
    with db.connect(_db_path()) as conn:
        conn.execute(_WATCHLIST_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (stock_id, added_at) VALUES (?,?)",
            (stock_id, dt.datetime.now().isoformat(timespec="seconds")),
        )
    return list_watchlist()


def remove_watchlist(stock_id: str) -> list[str]:
    """移除自選（冪等），回傳更新後清單。"""
    with db.connect(_db_path()) as conn:
        conn.execute(_WATCHLIST_DDL)
        conn.execute("DELETE FROM watchlist WHERE stock_id=?", (stock_id,))
    return list_watchlist()
