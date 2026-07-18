"""審查修復回歸測試：還原價雙重調整清理、月營收公告前視閘門（temp DB，不需連網）。"""
from __future__ import annotations

import pandas as pd

from src.data import corporate_actions
from src.data import database as db
from src.data import query as q


def _settings(dbfile):
    class S:
        db_path = dbfile
    return S()


def test_detect_cleans_capital_change_overlapping_dividend(tmp_path, monkeypatch):
    """#1：除息跳空曾被誤判成減資寫入 capital_change，與同日 dividend 疊乘造成
    還原價雙重調整。detect 應自清理（刪除與 dividend 同日的 capital_change）。"""
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    with db.connect(dbfile) as conn:
        # 同一 (2603, 2023-06-30) 在兩表都有：dividend 是權威、capital_change 是誤判
        conn.execute("INSERT INTO dividend (stock_id, date, before_price, after_price, dividend, kind) "
                     "VALUES ('2603','2023-06-30',155.0,85.0,70.0,'息')")
        conn.execute("INSERT INTO capital_change (stock_id, date, before_price, after_price, kind) "
                     "VALUES ('2603','2023-06-30',155.0,93.5,'auto_reduction')")
        # 另一筆純公司行動（無同日除息）應保留
        conn.execute("INSERT INTO capital_change (stock_id, date, before_price, after_price, kind) "
                     "VALUES ('2603','2022-08-01',60.0,20.0,'auto_split')")
        n = corporate_actions.detect(conn, stock_id="2603")
        rows = {r[0] for r in conn.execute(
            "SELECT date FROM capital_change WHERE stock_id='2603'").fetchall()}
    assert n == 0
    assert rows == {"2022-08-01"}          # 同日誤判被清、純行動保留


def test_adjustment_single_after_cleanup(tmp_path, monkeypatch):
    """#1：清理後還原係數只套一次（除息前價格 = 原始 × 單一係數，不疊乘）。"""
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    monkeypatch.setattr(q, "get_settings", lambda: _settings(dbfile))
    with db.connect(dbfile) as conn:
        px = pd.DataFrame([
            {"stock_id": "2603", "date": "2023-06-29", "open": 155, "high": 156,
             "low": 154, "close": 155, "volume": 1000},
            {"stock_id": "2603", "date": "2023-06-30", "open": 85, "high": 86,
             "low": 84, "close": 85, "volume": 1000},
        ])
        db.upsert_dataframe(conn, "price_daily", px)
        conn.execute("INSERT INTO dividend (stock_id, date, before_price, after_price, dividend, kind) "
                     "VALUES ('2603','2023-06-30',155.0,85.0,70.0,'息')")
        conn.execute("INSERT INTO capital_change (stock_id, date, before_price, after_price, kind) "
                     "VALUES ('2603','2023-06-30',155.0,93.5,'auto_reduction')")
        corporate_actions.detect(conn, stock_id="2603")
    adj = q.get_price("2603", adjusted=True)
    # 單一除息係數 85/155：除息前 155 × (85/155) = 85，連續無跳空（非雙重調整的更低值）
    pre = float(adj[adj["date"] == "2023-06-29"]["close"].iloc[0])
    assert abs(pre - 85.0) < 1e-6


def test_revenue_lookahead_gated_at_day10(tmp_path, monkeypatch):
    """#7：月營收在公告截止日（次月 10 日）前不可見，避免回測月初調倉前視。"""
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    monkeypatch.setattr(q, "get_settings", lambda: _settings(dbfile))
    with db.connect(dbfile) as conn:
        # 6 月營收，date 慣例＝次月 1 日（2024-07-01），實際公告截止 2024-07-10
        conn.execute("INSERT INTO month_revenue (stock_id, date, revenue_year, revenue_month, revenue) "
                     "VALUES ('2330','2024-07-01',2024,6,1000)")
    assert q.get_revenue_bulk(["2330"], "2024-07-01").empty   # 月初調倉：尚未公告
    assert q.get_revenue_bulk(["2330"], "2024-07-09").empty   # 公告日前一天
    assert not q.get_revenue_bulk(["2330"], "2024-07-10").empty  # 公告截止日：可見
