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


def test_fetch_tpex_dividend_parses_swagger_fields(conn):
    fixture = [
        {  # 正常列（欄位名依官方 swagger，含原文拼字 Diviend）
            "Date": "1150625", "SecuritiesCompanyCode": "5483",
            "CompanyName": "中美晶",
            "ClosePriceBeforeExRightsDiviend": "168.5",
            "ExRightsDiviendQuote": "161.5",
            "StockDividend": "0", "CashDividend": "7.0",
            "StockDividendPlusCashDividend": "7.0",
            "ExRightsDiviend": "息",
        },
        {  # 缺價格 → 應略過
            "Date": "1150626", "SecuritiesCompanyCode": "9999",
            "ClosePriceBeforeExRightsDiviend": "-",
            "ExRightsDiviendQuote": "", "ExRightsDiviend": "權",
        },
    ]
    resp = MagicMock(status_code=200)
    resp.json.return_value = fixture
    with patch.object(twse_source._session, "get", return_value=resp), \
         patch.object(twse_source.time, "sleep"):
        n = twse_source.fetch_tpex_dividend(conn)
    assert n == 1
    row = conn.execute("SELECT * FROM dividend WHERE stock_id='5483'").fetchone()
    assert row is not None
    sid, date, before, after, dividend, kind = row
    assert date == "2026-06-25"
    assert before == 168.5 and after == 161.5
    assert dividend == 7.0 and kind == "息"
