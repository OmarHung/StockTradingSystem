"""PaperBroker 單元測試：撮合/失效/停損/停利/損益/權益快照（temp DB + 假價格）。"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src.data import database as db


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """隔離的 PaperBroker：temp DB + 100 萬起始資金。"""
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)

    class FakeSettings:
        db_path = dbfile
        def __getitem__(self, k):
            return {"capital": {"total": 1_000_000}}[k]

    import src.broker.paper as paper_mod
    monkeypatch.setattr(paper_mod, "get_settings", lambda: FakeSettings())
    return paper_mod.PaperBroker()


def _px(open_, high, low, close):
    return pd.DataFrame([{"date": "2024-01-02", "open": open_, "high": high,
                          "low": low, "close": close}])


def test_fill_when_open_below_limit(broker):
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=115.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(101, 103, 100, 102)):
        res = broker.execute_pending("2024-01-02")
    assert res[0]["status"] == "filled" and res[0]["price"] == 101
    pos = broker.positions()
    assert len(pos) == 1 and int(pos.iloc[0]["shares"]) == 1000
    # 現金 = 100萬 - 10.1萬 - 手續費
    assert broker.cash == pytest.approx(1_000_000 - 101_000 - 101_000 * 0.001425, rel=1e-6)


def test_expire_when_gap_up(broker):
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=115.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(105, 106, 104, 105)):
        res = broker.execute_pending("2024-01-02")
    assert res[0]["status"] == "expired"
    assert broker.positions().empty and broker.cash == 1_000_000


def test_stop_loss_exit_with_pnl(broker):
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=115.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(100, 101, 99, 100)):
        broker.execute_pending("2024-01-02")           # 進場 @100
    with patch("src.broker.paper.q.get_price", return_value=_px(97, 98, 94, 95)):
        exits = broker.check_stops("2024-01-03")       # low 94 觸損 95
    assert exits[0]["reason"] == "stop" and exits[0]["price"] == 95
    assert broker.positions().empty
    fills = broker.fills()
    sell = fills[fills["side"] == "SELL"].iloc[0]
    assert sell["pnl"] < 0                              # 賠錢出場
    # 冷卻記錄（測試用合成日期在很久以前，放大查詢窗）
    assert broker.recent_stops(days=2000).get("9999") == "2024-01-03"


def test_target_exit(broker):
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=110.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(100, 101, 99, 100)):
        broker.execute_pending("2024-01-02")
    with patch("src.broker.paper.q.get_price", return_value=_px(108, 112, 107, 111)):
        exits = broker.check_stops("2024-01-03")       # high 112 ≥ 目標 110
    assert exits[0]["reason"] == "target"
    sell = broker.fills().iloc[0]
    assert sell["pnl"] > 0                              # 獲利了結


def test_mark_to_market(broker):
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=90.0, target=120.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(100, 101, 99, 100)):
        broker.execute_pending("2024-01-02")
    with patch("src.broker.paper.q.get_price", return_value=_px(104, 106, 103, 105)):
        snap = broker.mark_to_market("2024-01-03")
    # 持倉市值 1000×105；權益 = 現金 + 市值 > 100萬（浮盈 5000 - 手續費）
    assert snap["positions_value"] == 105_000
    assert snap["equity"] > 1_000_000


def test_emergency_toggle(broker):
    assert broker.trading_enabled() is True
    broker.set_trading_enabled(False)
    assert broker.trading_enabled() is False
    broker.set_trading_enabled(True)
    assert broker.trading_enabled() is True