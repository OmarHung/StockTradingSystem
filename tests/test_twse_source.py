"""官方源解析測試（離線 fixture，不打真實 API）。"""
from unittest.mock import MagicMock, patch

import pytest

from src.data import database as db
from src.data import twse_source


@pytest.fixture()
def conn(tmp_path):
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    c = db.get_connection(dbfile)
    yield c
    c.close()


def test_any_date_formats():
    assert twse_source._any_date("1150102") == "2026-01-02"     # 民國7碼
    assert twse_source._any_date("20260102") == "2026-01-02"    # 西元8碼
    assert twse_source._any_date("2026-01-02") == "2026-01-02"  # ISO
    assert twse_source._any_date("115/01/02") == "2026-01-02"   # 民國斜線
    assert twse_source._any_date("") is None
    assert twse_source._any_date("N/A") is None


def test_fetch_tpex_dividends_range_parses_exdailyq(conn):
    fixture = {"stat": "ok", "tables": [{
        "fields": ["除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價",
                   "權值", "息值", "權值+息值", "權/息", "漲停價", "跌停價",
                   "開始交易基準價", "減除股利參考價", "現金股利", "每仟股無償配股",
                   "現金增資股數", "現金增資認購價", "公開承銷股數", "員工認購股數",
                   "原股東認購股數", "按持股比例仟股認購"],
        "data": [
            ["115/06/25", "5483", "中美晶", "168.5", "161.5", "0.000000", "7.000000",
             "7.000000", "除息", "185.0", "145.5", "161.5", "161.5", "7.0", "0",
             "0", "0.00", "0", "0", "0", "0"],
            ["115/06/26", "9999", "壞資料", "-", "", "0", "0",
             "0", "除權", "", "", "", "", "", "", "", "", "", "", "", ""],  # 缺價格 → 略過
        ],
    }]}
    resp = MagicMock(status_code=200)
    resp.json.return_value = fixture
    with patch.object(twse_source._session, "post", return_value=resp), \
         patch.object(twse_source.time, "sleep"):
        n = twse_source.fetch_tpex_dividends_range(conn, "2026-06-01", "2026-06-30")
    assert n == 1
    row = conn.execute("SELECT * FROM dividend WHERE stock_id='5483'").fetchone()
    assert row is not None
    sid, date, before, after, dividend, kind = row
    assert date == "2026-06-25"
    assert before == 168.5 and after == 161.5
    assert dividend == 7.0 and kind == "除息"


def _tpex_inst_fixture():
    """TPEx dailyTrade 24 欄佈局（實測結構）：0代號 1名稱｜三組 買/賣/淨 ×8｜末欄合計。
    兩列皆自洽（買-賣=淨；外資合計+投信+自營合計=末欄合計）。"""
    fields = (["代號", "名稱"]
              + ["買進股數", "賣出股數", "買賣超股數"] * 7
              + ["三大法人買賣超股數合計"])
    data = [
        # 2-4 外資陸資 | 5-7 外資自營 | 8-10 外資合計 | 11-13 投信 |
        # 14-16 自營自行 | 17-19 自營避險 | 20-22 自營合計 | 23 合計
        ["2330", "台積電",
         "1,000", "400", "600",   "0", "0", "0",   "1,000", "400", "600",
         "300", "100", "200",   "50", "20", "30",   "10", "5", "5",   "60", "25", "35",
         "835"],
        ["00679B", "元大美債20年",
         "500", "500", "0",   "0", "0", "0",   "500", "500", "0",
         "0", "0", "0",   "20", "10", "10",   "0", "0", "0",   "20", "10", "10",
         "10"],
    ]
    return {"tables": [{"title": "三大法人買賣明細資訊", "fields": fields, "data": data}]}


def test_fetch_tpex_institutional_parses_groups(conn):
    fixture = _tpex_inst_fixture()
    resp = MagicMock(status_code=200)
    resp.json.return_value = fixture
    with patch.object(twse_source, "_get_json", return_value=fixture), \
         patch.object(twse_source.time, "sleep"):
        n = twse_source.fetch_tpex_institutional(conn, "2026-07-03")
    assert n == 6  # 2 檔 × 3 群組
    r = conn.execute("SELECT name, buy, sell FROM institutional "
                     "WHERE stock_id='2330' ORDER BY name").fetchall()
    assert r == [("Dealer_self", 50, 20),
                 ("Foreign_Investor", 1000, 400),
                 ("Investment_Trust", 300, 100)]


def test_fetch_tpex_institutional_rejects_drifted_layout(conn):
    """欄序漂移（買-賣≠淨）時整批跳過、不寫入猜測值。"""
    fixture = _tpex_inst_fixture()
    for row in fixture["tables"][0]["data"]:
        row[2], row[4] = row[4], row[2]  # 破壞 外資 買-賣=淨 冗餘關係
    resp = MagicMock(status_code=200)
    resp.json.return_value = fixture
    with patch.object(twse_source, "_get_json", return_value=fixture), \
         patch.object(twse_source.time, "sleep"):
        n = twse_source.fetch_tpex_institutional(conn, "2026-07-03")
    assert n == 0
    assert conn.execute("SELECT COUNT(*) FROM institutional").fetchone()[0] == 0


def test_fetch_twse_valuation_parses_fields(conn):
    fixture = {
        "stat": "OK",
        "fields": ["證券代號", "證券名稱", "收盤價", "殖利率(%)", "股利年度", "本益比", "股價淨值比", "財報年/季"],
        "data": [
            ["2330", "台積電", "1075.00", "0.90", 114, "32.87", "10.76", "115/1"],
            ["1101", "台泥", "24.60", "3.25", 114, "-", "0.82", "114/4"],  # 虧損：本益比 '-'
        ],
    }
    resp = MagicMock(status_code=200)
    resp.json.return_value = fixture
    with patch.object(twse_source._session, "get", return_value=resp), \
         patch.object(twse_source.time, "sleep"):
        n = twse_source.fetch_twse_valuation(conn, "2026-07-03")
    assert n == 2
    r = conn.execute("SELECT per, pbr, dividend_yield FROM valuation WHERE stock_id='2330'").fetchone()
    assert r == (32.87, 10.76, 0.9)
    r = conn.execute("SELECT per, pbr FROM valuation WHERE stock_id='1101'").fetchone()
    assert r[0] is None and r[1] == 0.82  # 虧損公司 PER 存 NULL


def test_fetch_tpex_valuation_parses_fields(conn):
    fixture = {"tables": [{
        "fields": ["股票代號", "公司名稱", "本益比", "每股股利", "股利年度", "殖利率(%)", "股價淨值比", "財報年/季"],
        "data": [["5483", "中美晶        ", "29.90", "7.00000000", 114, "1.66", "2.62", "115Q1"]],
    }]}
    resp = MagicMock(status_code=200)
    resp.json.return_value = fixture
    with patch.object(twse_source._session, "get", return_value=resp), \
         patch.object(twse_source.time, "sleep"):
        n = twse_source.fetch_tpex_valuation(conn, "2026-07-03")
    assert n == 1
    r = conn.execute("SELECT per, pbr, dividend_yield FROM valuation WHERE stock_id='5483'").fetchone()
    assert r == (29.9, 2.62, 1.66)


def test_fetch_dividend_forecast_both_markets(conn):
    twse = [{"Date": "1150716", "Code": "1102", "Name": "亞泥", "Exdividend": "息",
             "StockDividendRatio": "", "CashDividend": "2.300000"}]
    tpex = [{"ExRrightsExDividendDate": "1150720", "SecuritiesCompanyCode": "5483",
             "ExRrightsExDividend": "除息", "StockDividendRatio": "0.00000000",
             "CashDividend": "7.00000000"}]
    resps = [MagicMock(status_code=200), MagicMock(status_code=200)]
    resps[0].json.return_value = twse
    resps[1].json.return_value = tpex
    with patch.object(twse_source._session, "get", side_effect=resps), \
         patch.object(twse_source.time, "sleep"):
        n = twse_source.fetch_dividend_forecast(conn)
    assert n == 2
    r = conn.execute("SELECT date, kind, cash_dividend FROM dividend_forecast WHERE stock_id='1102'").fetchone()
    assert r == ("2026-07-16", "息", 2.3)
    r = conn.execute("SELECT date, kind, cash_dividend FROM dividend_forecast WHERE stock_id='5483'").fetchone()
    assert r == ("2026-07-20", "除息", 7.0)


def test_corporate_action_detector(conn):
    from src.data import corporate_actions
    import pandas as pd
    from src.data import database as db2
    # 15 天平穩 + 第 16 天 1拆4 跳空（-75%）；另一檔正常波動不觸發
    rows = []
    for i in range(15):
        rows.append({"stock_id": "9998", "date": f"2026-01-{i+1:02d}", "open": 100, "high": 101,
                     "low": 99, "close": 100 + i * 0.5, "volume": 1000})
    rows.append({"stock_id": "9998", "date": "2026-01-16", "open": 26, "high": 27,
                 "low": 25.5, "close": 26.6, "volume": 4000})
    for i in range(16):
        rows.append({"stock_id": "9997", "date": f"2026-01-{i+1:02d}", "open": 50, "high": 51,
                     "low": 49, "close": 50 + (i % 3), "volume": 1000})
    db2.upsert_dataframe(conn, "price_daily", pd.DataFrame(rows))
    conn.commit()
    n = corporate_actions.detect(conn)
    assert n == 1
    r = conn.execute("SELECT date, before_price, after_price, kind FROM capital_change "
                     "WHERE stock_id='9998'").fetchone()
    assert r[0] == "2026-01-16" and r[3] == "auto_split"
    assert conn.execute("SELECT COUNT(*) FROM capital_change WHERE stock_id='9997'").fetchone()[0] == 0
