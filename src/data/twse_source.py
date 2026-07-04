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
_THROTTLE = {"www.twse.com.tw": 3.0, "www.tpex.org.tw": 6.0,
             "mopsov.twse.com.tw": 3.0}
_last_call: dict[str, float] = {}


class OfficialSourceError(RuntimeError):
    pass


def _make_session() -> requests.Session:
    """TPEx 憑證缺 Subject Key Identifier 擴展，Python 3.13 起預設的
    VERIFY_X509_STRICT 會直接握手失敗（curl/瀏覽器都能連）。
    這裡只關掉 strict 旗標——憑證鏈與主機名驗證照常。"""
    import ssl
    from requests.adapters import HTTPAdapter

    class _LaxAdapter(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            ctx = ssl.create_default_context()
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            kw["ssl_context"] = ctx
            return super().init_poolmanager(*a, **kw)

    s = requests.Session()
    s.mount("https://www.tpex.org.tw/", _LaxAdapter())
    return s


_session = _make_session()


def _get_json(url: str) -> dict:
    """節流 GET（官方站禮貌頻率，逐主機獨立），非 JSON/非 200 拋錯。"""
    host = url.split("/")[2]
    throttle = _THROTTLE.get(host, 3.0)
    wait = throttle - (time.monotonic() - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()
    r = _session.get(url, headers=_HEADERS, timeout=30)
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

# ---------- MOPS 月營收（全市場單月一檔，千元）----------
def fetch_mops_revenue(conn, year: int, month: int, market: str) -> int:
    """抓 MOPS 月營收彙總（market: sii=上市 / otc=上櫃）。

    檔案：mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{民國年}_{月}_0.html
    一檔涵蓋該市場全部公司該月營收（千元，入庫 ×1000 對齊 FinMind 元制）。
    date 欄沿用 FinMind 慣例＝營收月次月 1 日（公告期為次月 10 日前）。
    """
    import datetime as _dt
    from io import StringIO

    global _last_call
    host = "mopsov.twse.com.tw"
    throttle = _THROTTLE.get(host, 3.0)
    wait = throttle - (time.monotonic() - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()

    url = (f"https://{host}/nas/t21/{market}/t21sc03_{year - 1911}_{month}_0.html")
    r = _session.get(url, headers=_HEADERS, timeout=30)
    if r.status_code == 404:
        return 0  # 該月檔案尚未產生（或過舊）
    if r.status_code != 200:
        raise OfficialSourceError(f"MOPS HTTP {r.status_code}: {url[:80]}")
    r.encoding = "big5"

    try:
        tables = pd.read_html(StringIO(r.text))
    except ValueError:
        return 0  # 頁面無表格

    # date 慣例：營收月的次月 1 日（與 FinMind 一致，供 as_of 過濾不前視）
    if month == 12:
        date_str = f"{year + 1}-01-01"
    else:
        date_str = f"{year}-{month + 1:02d}-01"

    rows = []
    for t in tables:
        if t.shape[1] < 3 or t.shape[0] < 1:
            continue
        # 攤平 MultiIndex 欄名後找「公司代號 + 當月營收」表
        cols = ["".join(str(x) for x in c) if isinstance(c, tuple) else str(c)
                for c in t.columns]
        if not any("公司" in c and "代號" in c for c in cols):
            continue
        if not any("當月營收" in c for c in cols):
            continue
        rev_idx = next(i for i, c in enumerate(cols) if "當月營收" in c)
        for _, row in t.iterrows():
            sid = str(row.iloc[0]).strip()
            if len(sid) != 4 or not sid.isdigit():
                continue  # 跳過小計/合計/表頭列
            rev = _fnum(row.iloc[rev_idx])
            if rev is None:
                continue
            rows.append({
                "stock_id": sid, "date": date_str,
                "revenue_year": year, "revenue_month": month,
                "revenue": int(rev * 1000),   # 千元 → 元（對齊 FinMind）
            })
    if not rows:
        return 0
    n = db.upsert_dataframe(conn, "month_revenue", pd.DataFrame(rows))
    now = _dt.datetime.now().isoformat(timespec="seconds")
    db.merge_range(conn, f"mops_{market}", f"{year:04d}-{month:02d}",
                   date_str, date_str, now)
    return n


# ---------- TPEx 除權息（bulletin/exDailyQ，日期區間、資料回溯 2008）----------
def fetch_tpex_dividends_range(conn, start_iso: str, end_iso: str) -> int:
    """上櫃除權息計算結果（上櫃版 TWT49U）。

    端點藏在新版 RWD 站（頁面 /zh-tw/announce/market/ex/cal.html 內嵌
    action="bulletin/exDailyQ"），必須用 POST；openapi 的 tpex_exright_daily
    只有近期快照不能回補，勿用。
    """
    global _last_call
    host = "www.tpex.org.tw"
    throttle = _THROTTLE.get(host, 6.0)
    wait = throttle - (time.monotonic() - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()

    r = _session.post(f"https://{host}/www/zh-tw/bulletin/exDailyQ",
                      data={"startDate": start_iso.replace("-", "/"),
                            "endDate": end_iso.replace("-", "/"),
                            "response": "json"},
                      headers=_HEADERS, timeout=30)
    if r.status_code != 200:
        raise OfficialSourceError(f"exDailyQ HTTP {r.status_code}")
    j = r.json()
    tables = j.get("tables") or []
    if str(j.get("stat")).lower() != "ok" or not tables or not tables[0].get("data"):
        return 0
    t = tables[0]
    fields = t.get("fields") or []

    def col(name_part: str) -> int:
        for i, f in enumerate(fields):
            if name_part in f:
                return i
        raise OfficialSourceError(f"exDailyQ 找不到欄位 {name_part}（欄位定義變更？）")

    di, ci = col("除權息日期"), col("代號")
    bi, ai = col("除權息前收盤價"), col("除權息參考價")
    vi, ki = col("權值+息值"), col("權/息")
    rows = []
    for row in t["data"]:
        sid = str(row[ci]).strip()
        date = _roc_date(row[di])
        before, after = _fnum(row[bi]), _fnum(row[ai])
        if not sid or not date or not before or not after:
            continue
        rows.append({"stock_id": sid, "date": date,
                     "before_price": before, "after_price": after,
                     "dividend": _fnum(row[vi]), "kind": str(row[ki]).strip()})
    if not rows:
        return 0
    return db.upsert_dataframe(conn, "dividend", pd.DataFrame(rows))


# ---------- 每日估值指標（本益比/殖利率/股價淨值比）----------
def fetch_twse_valuation(conn, date_iso: str) -> int:
    """TWSE BWIBBU_d：上市全市場單日估值（歷史按日可查）。"""
    ymd = date_iso.replace("-", "")
    j = _get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
                  f"?date={ymd}&selectType=ALL&response=json")
    if j.get("stat") != "OK" or not j.get("data"):
        return 0
    fields = j["fields"]

    def col(name_part: str) -> int:
        for i, f in enumerate(fields):
            if name_part in f:
                return i
        raise OfficialSourceError(f"BWIBBU_d 找不到欄位 {name_part}（欄位定義變更？）")

    ci, yi, pi, bi = col("證券代號"), col("殖利率"), col("本益比"), col("股價淨值比")
    rows = [{"stock_id": str(r[ci]).strip(), "date": date_iso,
             "per": _fnum(r[pi]), "pbr": _fnum(r[bi]), "dividend_yield": _fnum(r[yi])}
            for r in j["data"] if str(r[ci]).strip()]
    return db.upsert_dataframe(conn, "valuation", pd.DataFrame(rows))


def fetch_tpex_valuation(conn, date_iso: str) -> int:
    """TPEx peQryDate：上櫃全市場單日估值（歷史按日可查）。"""
    y, m, d = date_iso.split("-")
    j = _get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryDate"
                  f"?date={y}/{m}/{d}&response=json")
    tables = j.get("tables") or []
    if not tables or not tables[0].get("data"):
        return 0
    t = tables[0]
    fields = t.get("fields") or []

    def col(name_part: str) -> int:
        for i, f in enumerate(fields):
            if name_part in f:
                return i
        raise OfficialSourceError(f"peQryDate 找不到欄位 {name_part}（欄位定義變更？）")

    ci, pi, yi, bi = col("股票代號"), col("本益比"), col("殖利率"), col("股價淨值比")
    rows = [{"stock_id": str(r[ci]).strip(), "date": date_iso,
             "per": _fnum(r[pi]), "pbr": _fnum(r[bi]), "dividend_yield": _fnum(r[yi])}
            for r in t["data"] if str(r[ci]).strip()]
    return db.upsert_dataframe(conn, "valuation", pd.DataFrame(rows))


def _any_date(s) -> str | None:
    """openapi 日期容錯：民國7碼'1150102' / 西元8碼'20260102' /
    含分隔符民國（交給 _roc_date）/ ISO 'YYYY-MM-DD' 皆可。"""
    s = str(s or "").strip()
    if len(s) == 7 and s.isdigit():
        return _roc_date(f"{s[:3]}/{s[3:5]}/{s[5:7]}")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 10 and s[4] == "-":
        return s
    return _roc_date(s)
