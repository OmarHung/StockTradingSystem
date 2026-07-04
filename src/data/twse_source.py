"""TWSE/TPEx 官方資料源（免費、免金鑰、按日全市場）——籌碼與除權息主源。

殺手級優勢：一次呼叫回全市場（vs FinMind free 逐檔），日更 = 每天 4 次呼叫。

端點（實測驗證）：
- TWSE T86           三大法人買賣超（歷史按日，股數，深度至少 2020）
- TWSE MI_MARGN      融資融券（歷史按日，張）
- TWSE TWT49U        除權息計算結果（日期區間，含前後價 → 還原價直接可算）
- TPEx dailyTrade    上櫃三大法人（按日）
- TPEx margin/balance 上櫃融資券（按日）

工程注意：民國年、數字帶千分位逗號、欄位順序跨年份會變（一律按 fields 名稱對照，
TPEx 重複欄名則按群組位置）。官方無 SLA → 禮貌節流 3 秒/次。
"""
from __future__ import annotations

import time

import pandas as pd
import requests

from src.data import database as db
from src.logging_setup import get_logger

log = get_logger(__name__)

_HEADERS = {"accept": "application/json", "user-agent": "Mozilla/5.0"}
# 每主機獨立節流：TPEx 對高頻更敏感（實測連續抓會被暫時拒連），放慢
_THROTTLE = {"www.twse.com.tw": 3.0, "www.tpex.org.tw": 6.0}
_last_call: dict[str, float] = {}


class OfficialSourceError(RuntimeError):
    pass


def _get_json(url: str) -> dict:
    """節流 GET（官方站禮貌頻率，逐主機獨立），非 JSON/非 200 拋錯。"""
    host = url.split("/")[2]
    throttle = _THROTTLE.get(host, 3.0)
    wait = throttle - (time.monotonic() - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()
    r = requests.get(url, headers=_HEADERS, timeout=30)
    if r.status_code != 200:
        raise OfficialSourceError(f"HTTP {r.status_code}: {url[:80]}")
    try:
        return r.json()
    except Exception as e:  # noqa: BLE001
        raise OfficialSourceError(f"非 JSON 回應: {url[:80]}") from e


def _num(s) -> int:
    """'75,005,048' → 75005048；空/―/None → 0。"""
    if s is None:
        return 0
    s = str(s).replace(",", "").replace("--", "").strip()
    if not s or s in ("-", "―", "N/A"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _fnum(s) -> float | None:
    s = str(s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _roc_date(s: str) -> str | None:
    """'115年01月02日' / '115/01/02' → '2026-01-02'。"""
    s = str(s).replace("年", "/").replace("月", "/").replace("日", "").strip()
    parts = s.split("/")
    if len(parts) != 3:
        return None
    try:
        return f"{int(parts[0]) + 1911}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    except ValueError:
        return None


# ---------- TWSE 三大法人（T86）----------
def fetch_twse_institutional(conn, date_iso: str) -> int:
    ymd = date_iso.replace("-", "")
    j = _get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86"
                  f"?date={ymd}&selectType=ALLBUT0999&response=json")
    if j.get("stat") != "OK" or not j.get("data"):
        return 0
    fields = j["fields"]

    def col(name_part: str) -> int:
        for i, f in enumerate(fields):
            if name_part in f:
                return i
        raise OfficialSourceError(f"T86 找不到欄位 {name_part}（欄位定義變更？）")

    # FinMind 相容的法人名稱對照（下游 chips features 不用改）
    mapping = [
        ("Foreign_Investor", col("外陸資買進股數(不含外資自營商)"), col("外陸資賣出股數(不含外資自營商)")),
        ("Foreign_Dealer_Self", col("外資自營商買進股數"), col("外資自營商賣出股數")),
        ("Investment_Trust", col("投信買進股數"), col("投信賣出股數")),
        ("Dealer_self", col("自營商買進股數(自行買賣)"), col("自營商賣出股數(自行買賣)")),
        ("Dealer_Hedging", col("自營商買進股數(避險)"), col("自營商賣出股數(避險)")),
    ]
    # 舊版 16 欄佈局（官方會混在 19 欄回應裡，每日約 40+ 檔，含宏碁等大型股）。
    # 佈局經算術驗證（淨額=買-賣、三法人合計相符）：
    # [2,3]外資買/賣 [5,6]投信 [9,10]自營自行 [12,13]自營避險（無外資自營細項）
    legacy16 = [
        ("Foreign_Investor", 2, 3), ("Investment_Trust", 5, 6),
        ("Dealer_self", 9, 10), ("Dealer_Hedging", 12, 13),
    ]
    rows, skipped, skipped_lens = [], 0, set()
    nf = len(fields)
    for row in j["data"]:
        sid = str(row[0]).strip()
        if len(row) == nf:
            use = mapping
        elif len(row) == 16:
            use = legacy16
        else:
            skipped += 1
            skipped_lens.add(len(row))
            continue
        for name, bi, si in use:
            rows.append({"stock_id": sid, "date": date_iso, "name": name,
                         "buy": _num(row[bi]), "sell": _num(row[si])})
    if skipped:
        log.info("T86 %s 跳過 %d 列未知欄數 %s（fields=%d）",
                 date_iso, skipped, sorted(skipped_lens), nf)
    return db.upsert_dataframe(conn, "institutional", pd.DataFrame(rows))


# ---------- TPEx 三大法人 ----------
def fetch_tpex_institutional(conn, date_iso: str) -> int:
    y, m, d = date_iso.split("-")
    j = _get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
                  f"?type=Daily&sect=EW&date={y}/{m}/{d}&response=json")
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        return 0
    data = tables[0]["data"]
    # 欄位重複名（外資/投信/自營各一組買進/賣出/買賣超），按群組位置取：
    # 0代號 1名稱 | 2-4 外資(買/賣/淨) ... 投信與自營位置依欄數推（歷年欄數不同，防禦性處理）
    ncol = len(data[0])
    # 常見版型：外資及陸資(不含自營) 2-4、外資自營 5-7、外資合計 8-10、投信 11-13、
    # 自營自行 14-16、自營避險 17-19…；較舊版可能沒有細分。取保守對照：
    if ncol >= 14:
        groups = [("Foreign_Investor", 2, 3), ("Investment_Trust", 11, 12),
                  ("Dealer_self", 14, 15)]
    else:  # 精簡版型：外資 2-4、投信 5-7、自營 8-10
        groups = [("Foreign_Investor", 2, 3), ("Investment_Trust", 5, 6),
                  ("Dealer_self", 8, 9)]
    rows = []
    for row in data:
        sid = str(row[0]).strip()
        if not sid or len(sid) > 6:
            continue
        for name, bi, si in groups:
            if si < len(row):
                rows.append({"stock_id": sid, "date": date_iso, "name": name,
                             "buy": _num(row[bi]), "sell": _num(row[si])})
    return db.upsert_dataframe(conn, "institutional", pd.DataFrame(rows))


# ---------- TWSE 融資融券（MI_MARGN）----------
def fetch_twse_margin(conn, date_iso: str) -> int:
    ymd = date_iso.replace("-", "")
    j = _get_json(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
                  f"?date={ymd}&selectType=ALL&response=json")
    table = None
    for t in j.get("tables") or []:
        if "融資融券彙總" in (t.get("title") or ""):
            table = t
            break
    if not table or not table.get("data"):
        return 0
    # fields: 代號 名稱 | 融資: 買進 賣出 現金償還 前日餘額 今日餘額 限額 | 融券: 買進 賣出 現券償還 前日餘額 今日餘額 限額
    rows = []
    for r in table["data"]:
        if len(r) < 13:
            continue
        rows.append({
            "stock_id": str(r[0]).strip(), "date": date_iso,
            "margin_purchase_buy": _num(r[2]), "margin_purchase_sell": _num(r[3]),
            "margin_purchase_balance": _num(r[6]),
            "short_sale_buy": _num(r[8]), "short_sale_sell": _num(r[9]),
            "short_sale_balance": _num(r[12]),
        })
    return db.upsert_dataframe(conn, "margin", pd.DataFrame(rows))


# ---------- TPEx 融資融券 ----------
def fetch_tpex_margin(conn, date_iso: str) -> int:
    y, m, d = date_iso.split("-")
    j = _get_json(f"https://www.tpex.org.tw/www/zh-tw/margin/balance"
                  f"?date={y}/{m}/{d}&response=json")
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        return 0
    # fields: 代號 名稱 前資餘額 資買 資賣 現償 資餘額 ... 前券餘額 券賣 券買 券償 券餘額 ...
    fields = tables[0].get("fields") or []

    def col(part, default):
        for i, f in enumerate(fields):
            if part in f:
                return i
        return default

    i_rb, i_rs = col("資買", 3), col("資賣", 4)
    i_rbal = col("資餘額", 6)
    i_sb, i_ss = col("券買", 10), col("券賣", 9)
    i_sbal = col("券餘額", 11)
    max_idx = max(i_rb, i_rs, i_rbal, i_sb, i_ss, i_sbal)
    rows = []
    for r in tables[0]["data"]:
        sid = str(r[0]).strip()
        if not sid or len(sid) > 6 or len(r) <= max_idx:
            continue
        rows.append({
            "stock_id": sid, "date": date_iso,
            "margin_purchase_buy": _num(r[i_rb]), "margin_purchase_sell": _num(r[i_rs]),
            "margin_purchase_balance": _num(r[i_rbal]),
            "short_sale_buy": _num(r[i_sb]), "short_sale_sell": _num(r[i_ss]),
            "short_sale_balance": _num(r[i_sbal]),
        })
    return db.upsert_dataframe(conn, "margin", pd.DataFrame(rows))


# ---------- 除權息（TWT49U，日期區間全市場）----------
def fetch_dividends_range(conn, start_iso: str, end_iso: str) -> int:
    j = _get_json(f"https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
                  f"?startDate={start_iso.replace('-', '')}"
                  f"&endDate={end_iso.replace('-', '')}&response=json")
    if j.get("stat") != "OK" or not j.get("data"):
        return 0
    rows = []
    for r in j["data"]:
        date = _roc_date(r[0])
        before, after = _fnum(r[3]), _fnum(r[4])
        if not date or not before or not after:
            continue
        rows.append({"stock_id": str(r[1]).strip(), "date": date,
                     "before_price": before, "after_price": after,
                     "dividend": _fnum(r[5]), "kind": str(r[6]).strip()})
    return db.upsert_dataframe(conn, "dividend", pd.DataFrame(rows))


# ---------- 綜合：某日全市場籌碼 ----------
def fetch_chips_for_date(conn, date_iso: str) -> dict:
    """抓某交易日 上市+上櫃 的法人與融資券。回傳各源筆數；單源失敗記 0 不中斷。"""
    out = {"twse_inst": 0, "tpex_inst": 0, "twse_margin": 0, "tpex_margin": 0}
    for key, fn in (("twse_inst", fetch_twse_institutional),
                    ("tpex_inst", fetch_tpex_institutional),
                    ("twse_margin", fetch_twse_margin),
                    ("tpex_margin", fetch_tpex_margin)):
        try:
            out[key] = fn(conn, date_iso)
        except Exception as e:  # noqa: BLE001
            log.warning("官方源 %s %s 失敗：%s", key, date_iso, str(e)[:100])
    return out