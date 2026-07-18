"""台股交易日曆（前瞻判斷「某天有沒有開盤」，含假日表同步）。

與 query.trading_calendar()（TAIEX 日K 回顧「過去哪些天開過盤」）互補：
這裡回答的是未來——掛隔日委託、排程觸發前，先知道明天/今天是不是交易日。

資料源：TWSE openapi holidaySchedule（官方年度休市表，免金鑰，僅提供當年度）。
回應混雜「開始交易日/最後交易日」等交易日提示項目，須過濾；日期為民國 7 碼。
判斷邏輯：週六日 → 休市；在假日表 → 休市；其餘 → 交易日。
該年度假日表未同步時退回平日判斷（＝舊行為，不會更差），並可 lazy 同步。

已知限制：颱風臨時休市不在年度表內——撮合層另以「TAIEX 當日無日K」兜底
（見 PaperBroker.execute_pending），假日表漏了也不會誤殺委託單。
"""
from __future__ import annotations

import datetime as dt

import requests

from src.config import get_settings
from src.data import database as db
from src.logging_setup import get_logger

log = get_logger(__name__)

_HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
_HEADERS = {"accept": "application/json", "user-agent": "Mozilla/5.0"}
_FETCH_DATASET = "twse_holiday"          # fetch_log 年度覆蓋標記（stock_id＝西元年）
_sync_attempted: set[int] = set()        # 每行程每年只 lazy 同步一次，失敗不重打


def _roc7(s) -> str | None:
    """民國 7 碼 '1150101' → '2026-01-01'；非法回 None。"""
    s = str(s or "").strip()
    if len(s) != 7 or not s.isdigit():
        return None
    return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"


def _ensure_tables(conn) -> None:
    """防禦性建表：舊 DB 未跑 init_db 也能查（空表 → 自然退回平日判斷）。"""
    conn.execute(db.SCHEMA["market_holiday"])
    conn.execute(db.SCHEMA["fetch_log"])


def _covered(conn, year: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM fetch_log WHERE dataset=? AND stock_id=?",
        (_FETCH_DATASET, str(year))).fetchone() is not None


def sync_holidays(conn=None) -> dict:
    """抓 TWSE 年度休市表入庫（冪等）。回傳 {year: 休市日數}。

    過濾規則：Name 含「交易日」者（開始交易日/最後交易日）是交易日提示，
    不是休市日；其餘（放假、市場無交易僅結算交割、補假）皆為休市日。
    """
    if conn is None:
        with db.connect(get_settings().db_path) as c:
            return sync_holidays(c)
    r = requests.get(_HOLIDAY_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    now = dt.datetime.now().isoformat(timespec="seconds")
    per_year: dict[int, int] = {}
    for item in r.json():
        name = str(item.get("Name", "")).strip()
        date = _roc7(item.get("Date"))
        if not date or "交易日" in name:
            continue
        _ensure_tables(conn)
        conn.execute(
            "INSERT OR REPLACE INTO market_holiday (date, name, fetched_at) VALUES (?,?,?)",
            (date, name, now))
        per_year[int(date[:4])] = per_year.get(int(date[:4]), 0) + 1
    for year in per_year:
        db.merge_range(conn, _FETCH_DATASET, str(year),
                       f"{year}-01-01", f"{year}-12-31", now)
    if per_year:
        log.info("假日表同步完成：%s",
                 "、".join(f"{y} 年 {n} 個休市日" for y, n in sorted(per_year.items())))
    else:
        log.warning("假日表同步回應無有效休市日（API 格式變更？）")
    return {str(y): n for y, n in per_year.items()}


def _maybe_sync(conn, year: int) -> None:
    """查詢當年度且尚未覆蓋時，lazy 同步一次（失敗只記 log，判斷退回平日）。"""
    if year != dt.date.today().year or year in _sync_attempted or _covered(conn, year):
        return
    _sync_attempted.add(year)
    try:
        sync_holidays(conn)
    except Exception as e:  # noqa: BLE001
        log.warning("假日表 lazy 同步失敗（退回平日判斷）：%s", str(e)[:100])


def is_trading_day(date_str: str, allow_fetch: bool = True) -> bool:
    """date_str（YYYY-MM-DD）是否為台股交易日。

    週末 → False；假日表命中 → False；其餘 True。該年度未同步時等同平日判斷。
    allow_fetch=False：純查 DB 不打網路（排程器 asyncio 迴圈內用，避免阻塞）。
    """
    d = dt.date.fromisoformat(date_str)
    if d.weekday() >= 5:
        return False
    with db.connect(get_settings().db_path) as conn:
        _ensure_tables(conn)
        if allow_fetch:
            _maybe_sync(conn, d.year)
        return _is_trading_day_conn(conn, date_str)


def _is_trading_day_conn(conn, date_str: str) -> bool:
    """交易日判斷（用已開啟的連線；週末→False、假日表命中→False）。"""
    if dt.date.fromisoformat(date_str).weekday() >= 5:
        return False
    return conn.execute("SELECT 1 FROM market_holiday WHERE date=?",
                        (date_str,)).fetchone() is None


def next_trading_day(date_str: str, allow_fetch: bool = True) -> str:
    """date_str 之後（不含當天）的下一個交易日——隔日委託的預計撮合日。

    整段共用一條連線（原本每天各開一條，春節連假可連開近 10 條）。
    """
    d = dt.date.fromisoformat(date_str)
    with db.connect(get_settings().db_path) as conn:
        _ensure_tables(conn)
        if allow_fetch:
            _maybe_sync(conn, d.year)
        for _ in range(30):   # 春節連假最長也遠小於 30 天，防呆上限
            d += dt.timedelta(days=1)
            if _is_trading_day_conn(conn, d.isoformat()):
                return d.isoformat()
    return d.isoformat()


def status() -> dict:
    """日曆狀態（供 WebUI）：今天是否交易日、下一交易日、覆蓋年度、近期休市日。"""
    today = dt.date.today().isoformat()
    with db.connect(get_settings().db_path) as conn:
        _ensure_tables(conn)
        years = [r[0] for r in conn.execute(
            "SELECT stock_id FROM fetch_log WHERE dataset=? ORDER BY stock_id",
            (_FETCH_DATASET,)).fetchall()]
        upcoming = [{"date": r[0], "name": r[1]} for r in conn.execute(
            "SELECT date, name FROM market_holiday WHERE date>=? ORDER BY date LIMIT 10",
            (today,)).fetchall()]
    return {
        "today": today,
        "is_trading_day": is_trading_day(today, allow_fetch=False),
        "next_trading_day": next_trading_day(today, allow_fetch=False),
        "covered_years": years,
        "upcoming_holidays": upcoming,
    }
