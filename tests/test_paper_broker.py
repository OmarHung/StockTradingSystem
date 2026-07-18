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
    import src.data.market_calendar as mcal_mod
    monkeypatch.setattr(paper_mod, "get_settings", lambda: FakeSettings())
    # 交易日曆也指向 temp DB，並停用 lazy 網路同步（測試不打 TWSE API）
    monkeypatch.setattr(mcal_mod, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(mcal_mod, "_maybe_sync", lambda conn, year: None)
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


def test_fill_at_limit_when_intraday_touches(broker):
    """開盤高於限價，但盤中最低觸價 → 以限價成交（南亞情境）。"""
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=180.0,
                     stop_loss=160.0, target=220.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(186.5, 193, 174, 181.5)):
        res = broker.execute_pending("2024-01-02")
    assert res[0]["status"] == "filled" and res[0]["price"] == 180.0
    pos = broker.positions()
    assert len(pos) == 1 and float(pos.iloc[0]["avg_cost"]) == 180.0


def test_same_day_order_not_matched(broker):
    """同日重複執行流程：當天新掛的單留待次日撮合，不被當天行情誤殺。"""
    broker.place_buy("2024-01-02", "9999", 1000, limit_price=180.0,
                     stop_loss=160.0, target=220.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(186.5, 193, 174, 181.5)):
        res = broker.execute_pending("2024-01-02")   # 撮合日 = 掛單日
    assert res == []                                  # 未參與撮合
    assert broker.pending_orders().shape[0] == 1      # 仍為待撮合
    # 次交易日才撮合（盤中觸價 → 以限價成交）
    with patch("src.broker.paper.q.get_price", return_value=_px(186.5, 193, 174, 181.5)):
        res2 = broker.execute_pending("2024-01-03")
    assert res2[0]["status"] == "filled" and res2[0]["price"] == 180.0


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

def test_intraday_exit(broker):
    """盤中出場：平倉、費稅損益、現金入帳；不存在的持股回 None（冪等）。"""
    with db.connect(broker.db_path) as conn:
        conn.execute("INSERT INTO positions (stock_id, shares, avg_cost, stop_loss, target, opened_at) "
                     "VALUES ('2330', 1000, 100.0, 95.0, 120.0, '2026-07-01')")
    cash0 = broker.cash
    r = broker.intraday_exit("2026-07-05", "2330", 95.0, "stop_intraday")
    assert r is not None and r["reason"] == "stop_intraday" and r["shares"] == 1000
    assert broker.positions().empty
    assert broker.cash > cash0          # 賣出入帳：95000 - 費稅
    assert broker.fills().iloc[0]["reason"] == "stop_intraday"
    # 再出場同一檔 → None（收盤 check_stops 不會重複出場）
    assert broker.intraday_exit("2026-07-05", "2330", 95.0, "stop") is None


def test_place_buy_rejected_when_trading_disabled(broker):
    """緊急停止後 place_buy 拒單（kill-switch 掛單當下即時檢查）。"""
    broker.set_trading_enabled(False)
    oid = broker.place_buy("2026-07-06", "2330", 1000, 100.0, 95.0, 110.0)
    assert oid is None
    broker.set_trading_enabled(True)
    oid = broker.place_buy("2026-07-06", "2330", 1000, 100.0, 95.0, 110.0)
    assert isinstance(oid, int)


def test_expected_fill_date_skips_weekend_and_holiday(broker):
    """掛單記預計撮合日：跳過週末與假日表休市日。"""
    with db.connect(broker.db_path) as conn:
        # 2024-01-05 是週五；設 01-08（週一）為假日 → 預計撮合 01-09（週二）
        conn.execute("INSERT INTO market_holiday (date, name) VALUES ('2024-01-08', '測試假日')")
    broker.place_buy("2024-01-05", "9999", 1000, limit_price=100.0,
                     stop_loss=95.0, target=110.0)
    o = broker.pending_orders().iloc[0]
    assert o["expected_fill_date"] == "2024-01-09"


def test_execute_pending_skipped_on_holiday(broker):
    """休市日撮合：不動任何單（否則「當日無資料→失效」會誤殺全部委託）。"""
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=115.0)
    with db.connect(broker.db_path) as conn:
        conn.execute("INSERT INTO market_holiday (date, name) VALUES ('2024-01-02', '測試假日')")
    with patch("src.broker.paper.q.get_price", return_value=_px(101, 103, 100, 102)):
        res = broker.execute_pending("2024-01-02")   # 假日
        assert res == []
        res = broker.execute_pending("2024-01-06")   # 週六
        assert res == []
    assert broker.pending_orders().shape[0] == 1     # 委託保留
    # 下一個交易日照常撮合
    with patch("src.broker.paper.q.get_price", return_value=_px(101, 103, 100, 102)):
        res = broker.execute_pending("2024-01-03")
    assert res[0]["status"] == "filled"


def test_execute_pending_skipped_when_no_taiex_data(broker):
    """TAIEX 當日無日K（颱風臨時休市/價格未回補）→ 撮合跳過、委託保留。"""
    import pandas as pd
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=115.0)

    def _no_taiex(stock_id, *a, **kw):
        return pd.DataFrame() if stock_id == "TAIEX" else _px(101, 103, 100, 102)

    with patch("src.broker.paper.q.get_price", side_effect=_no_taiex):
        res = broker.execute_pending("2024-01-02")
    assert res == []
    assert broker.pending_orders().shape[0] == 1


def test_recent_stops_includes_intraday(broker):
    """冷卻閘要涵蓋盤中停損（stop_intraday）：盤中監控是主要停損路徑，
    只認 'stop' 會讓絕大多數停損漏進冷卻。"""
    with db.connect(broker.db_path) as conn:
        conn.execute("INSERT INTO positions (stock_id, shares, avg_cost, stop_loss, target, opened_at) "
                     "VALUES ('2330', 1000, 100.0, 95.0, 120.0, '2024-01-01')")
    broker.intraday_exit("2024-01-05", "2330", 95.0, "stop_intraday")
    assert broker.recent_stops(days=2000).get("2330") == "2024-01-05"
    # as_of 為基準：停損日之後才算冷卻中，之前不算（歷史重放不看未來停損）
    assert broker.recent_stops(days=2000, as_of="2024-01-04") == {}
    assert broker.recent_stops(days=2000, as_of="2024-01-05").get("2330") == "2024-01-05"


def test_kill_switch_cancels_pending_on_execute(broker):
    """緊急停止後，昨日委託在開盤撮合時被撤銷、不成交建倉（kill-switch 補防）。"""
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=115.0)
    broker.set_trading_enabled(False)
    with patch("src.broker.paper.q.get_price", return_value=_px(100, 101, 99, 100)):
        res = broker.execute_pending("2024-01-02")   # 開盤 100 ≤ 限價，正常會成交
    assert res and res[0]["status"] == "cancelled_kill_switch"
    assert broker.positions().empty                  # 沒建倉
    assert broker.pending_orders().empty             # 委託已撤
    assert broker.cash == 1_000_000                  # 現金未動


def test_stop_gap_down_fills_at_open(broker):
    """開盤跳空跌破停損：以開盤價成交（停損價當日不存在，否則虧損被低估）。"""
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=130.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(100, 101, 99, 100)):
        broker.execute_pending("2024-01-02")          # 進場 @100
    # 隔日開盤 90（已低於停損 95），全日 88~91 → 停損價 95 是當日不存在的價位
    with patch("src.broker.paper.q.get_price", return_value=_px(90, 91, 88, 89)):
        exits = broker.check_stops("2024-01-03")
    assert exits[0]["reason"] == "stop"
    assert exits[0]["price"] == 90                    # 開盤價，非停損價 95


def test_target_gap_up_fills_at_open(broker):
    """開盤跳空突破停利：以開盤價成交（實際賣得比目標價更好）。"""
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=90.0, target=110.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(100, 101, 99, 100)):
        broker.execute_pending("2024-01-02")
    # 隔日開盤 120（已高於停利 110）
    with patch("src.broker.paper.q.get_price", return_value=_px(120, 122, 118, 121)):
        exits = broker.check_stops("2024-01-03")
    assert exits[0]["reason"] == "target"
    assert exits[0]["price"] == 120                   # 開盤價，非目標價 110


def test_add_position_keeps_higher_stop(broker):
    """加碼不下移保護：合併部位停損取較高者、停利取較高者，新單缺值保留舊值。"""
    # 第一筆：停損 95、停利 115
    broker.place_buy("2024-01-01", "9999", 1000, limit_price=102.0,
                     stop_loss=95.0, target=115.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(100, 101, 99, 100)):
        broker.execute_pending("2024-01-02")
    # 第二筆加碼：停損較低 90（不該覆蓋）、停利較高 130（採用）
    broker.place_buy("2024-01-02", "9999", 1000, limit_price=105.0,
                     stop_loss=90.0, target=130.0)
    with patch("src.broker.paper.q.get_price", return_value=_px(103, 104, 102, 103)):
        broker.execute_pending("2024-01-03")
    pos = broker.positions().iloc[0]
    assert float(pos["stop_loss"]) == 95.0    # 保留較高停損（不下移保護位）
    assert float(pos["target"]) == 130.0      # 採較高停利
    assert int(pos["shares"]) == 2000


def test_sell_fee_min_20(broker):
    """賣出手續費套最低 20 元（小額出場不被低估，與 CostModel/回測一致）。"""
    with db.connect(broker.db_path) as conn:
        # 極小額持股：1 股 @10 元，賣出金額 10 元 → 手續費理論值 0.014 元，應夾到 20
        conn.execute("INSERT INTO positions (stock_id, shares, avg_cost, stop_loss, target, opened_at) "
                     "VALUES ('9999', 1, 10.0, 9.0, 20.0, '2024-01-01')")
    r = broker.intraday_exit("2024-01-05", "9999", 9.0, "stop_intraday")
    fee = broker.fills().iloc[0]["fee"]
    assert float(fee) == 20.0
    # pnl 已扣最低手續費（賣 9 元 - 成本 10 元 - 費 20 - 稅）
    assert r["pnl"] < 0
