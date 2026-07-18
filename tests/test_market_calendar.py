"""market_calendar 單元測試：假日表同步過濾、交易日判斷、次交易日推算（temp DB + 假 API）。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.data import database as db


@pytest.fixture
def cal(tmp_path, monkeypatch):
    """隔離的 market_calendar：temp DB、停用 lazy 網路同步。"""
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)

    class FakeSettings:
        db_path = dbfile

    import src.data.market_calendar as mcal
    monkeypatch.setattr(mcal, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(mcal, "_sync_attempted", set())
    return mcal


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


# TWSE openapi 樣本（民國 7 碼；混雜「開始/最後交易日」提示項目，須過濾）
_SAMPLE = [
    {"Name": "中華民國開國紀念日", "Date": "1150101", "Description": "依規定放假1日。"},
    {"Name": "國曆新年開始交易日", "Date": "1150102", "Description": "國曆新年開始交易。"},
    {"Name": "農曆春節前最後交易日", "Date": "1150211", "Description": "農曆春節前最後交易。"},
    {"Name": "市場無交易，僅辦理結算交割作業", "Date": "1150212", "Description": ""},
    {"Name": "農曆除夕及春節", "Date": "1150216", "Description": "放假5日。"},
]


def test_sync_filters_trading_day_hints(cal):
    with patch("src.data.market_calendar.requests.get", return_value=_FakeResp(_SAMPLE)):
        out = cal.sync_holidays()
    assert out == {"2026": 3}   # 兩筆「交易日」提示被過濾
    with db.connect(cal.get_settings().db_path) as conn:
        dates = {r[0] for r in conn.execute("SELECT date FROM market_holiday")}
        assert dates == {"2026-01-01", "2026-02-12", "2026-02-16"}
        # 年度覆蓋標記寫入 fetch_log
        assert conn.execute("SELECT 1 FROM fetch_log WHERE dataset='twse_holiday' "
                            "AND stock_id='2026'").fetchone()


def test_is_trading_day(cal):
    with patch("src.data.market_calendar.requests.get", return_value=_FakeResp(_SAMPLE)):
        cal.sync_holidays()
    assert cal.is_trading_day("2026-01-01", allow_fetch=False) is False  # 假日（週四）
    assert cal.is_trading_day("2026-01-02", allow_fetch=False) is True   # 交易日（週五）
    assert cal.is_trading_day("2026-01-03", allow_fetch=False) is False  # 週六
    # 未覆蓋年度：固定國定假日仍兜底為休市，其餘退回平日判斷
    assert cal.is_trading_day("2024-01-01", allow_fetch=False) is False  # 元旦（_FIXED_HOLIDAYS 兜底）
    assert cal.is_trading_day("2024-01-08", allow_fetch=False) is True   # 週一（平日，非固定假日）
    assert cal.is_trading_day("2024-01-06", allow_fetch=False) is False  # 週六


def test_next_trading_day_skips_weekend_and_holidays(cal):
    with patch("src.data.market_calendar.requests.get", return_value=_FakeResp(_SAMPLE)):
        cal.sync_holidays()
    # 2025-12-31（週三）→ 隔天 2026-01-01 假日 → 01-02 週五
    assert cal.next_trading_day("2025-12-31", allow_fetch=False) == "2026-01-02"
    # 2026-01-02（週五）→ 跳過週末 → 01-05 週一
    assert cal.next_trading_day("2026-01-02", allow_fetch=False) == "2026-01-05"


def test_sync_idempotent(cal):
    with patch("src.data.market_calendar.requests.get", return_value=_FakeResp(_SAMPLE)):
        cal.sync_holidays()
        cal.sync_holidays()   # 重跑不產生重複列
    with db.connect(cal.get_settings().db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM market_holiday").fetchone()[0]
    assert n == 3


def test_ensure_synced_populates_uncovered_years(cal):
    # 全新安裝：假日表未入庫，ensure_synced 主動同步指定年度
    with patch("src.data.market_calendar.requests.get", return_value=_FakeResp(_SAMPLE)):
        cal.ensure_synced(2026)
    with db.connect(cal.get_settings().db_path) as conn:
        assert conn.execute("SELECT 1 FROM fetch_log WHERE dataset='twse_holiday' "
                            "AND stock_id='2026'").fetchone()


def test_ensure_synced_fail_open_on_network_error(cal):
    # 無網路：同步失敗只記 WARNING、不拋出，呼叫端（排程迴圈）不被卡死
    with patch("src.data.market_calendar.requests.get",
               side_effect=OSError("no network")):
        cal.ensure_synced(2026)   # 不應拋例外
    with db.connect(cal.get_settings().db_path) as conn:
        assert conn.execute("SELECT 1 FROM fetch_log WHERE dataset='twse_holiday' "
                            "AND stock_id='2026'").fetchone() is None


def test_ensure_synced_retry_reattempts_failed_year(cal):
    # 啟動時失敗（無網路）→ 每行程每年只試一次；retry=True 可重試先前失敗年度
    with patch("src.data.market_calendar.requests.get",
               side_effect=OSError("no network")):
        cal.ensure_synced(2026)
    # 不帶 retry：年度已在 _sync_attempted，不再打網路（維持既有 lazy 契約）
    with patch("src.data.market_calendar.requests.get",
               return_value=_FakeResp(_SAMPLE)) as g:
        cal.ensure_synced(2026)
        assert g.call_count == 0
    # retry=True：清掉未覆蓋年度標記後重試，成功入庫
    with patch("src.data.market_calendar.requests.get",
               return_value=_FakeResp(_SAMPLE)):
        cal.ensure_synced(2026, retry=True)
    with db.connect(cal.get_settings().db_path) as conn:
        assert conn.execute("SELECT 1 FROM fetch_log WHERE dataset='twse_holiday' "
                            "AND stock_id='2026'").fetchone()


def test_status(cal):
    with patch("src.data.market_calendar.requests.get", return_value=_FakeResp(_SAMPLE)):
        cal.sync_holidays()
    s = cal.status()
    assert s["covered_years"] == ["2026"]
    assert isinstance(s["is_trading_day"], bool)
    assert s["next_trading_day"] > s["today"]
