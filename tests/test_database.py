"""資料庫層單元測試（不需連網）。用 pytest 執行：

    .venv/bin/python -m pytest tests/ -v
"""
from __future__ import annotations

import pandas as pd

from src.data import database as db


def test_init_and_upsert_idempotent(tmp_path):
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)

    df = pd.DataFrame(
        [
            {"stock_id": "2330", "date": "2024-01-02", "open": 590.0, "high": 593.0,
             "low": 589.0, "close": 593.0, "volume": 27997826},
            {"stock_id": "2330", "date": "2024-01-03", "open": 592.0, "high": 595.0,
             "low": 590.0, "close": 594.0, "volume": 20000000},
        ]
    )

    with db.connect(dbfile) as conn:
        n1 = db.upsert_dataframe(conn, "price_daily", df)
    assert n1 == 2

    # 重跑同資料：主鍵衝突 → REPLACE，不新增列（冪等）
    with db.connect(dbfile) as conn:
        db.upsert_dataframe(conn, "price_daily", df)
        got = db.read_sql(conn, "SELECT COUNT(*) AS c FROM price_daily")
    assert int(got.iloc[0]["c"]) == 2


def test_upsert_ignores_extra_columns(tmp_path):
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    df = pd.DataFrame([{"stock_id": "2330", "date": "2024-01-02", "close": 593.0,
                        "unused_col": "x"}])
    with db.connect(dbfile) as conn:
        n = db.upsert_dataframe(conn, "price_daily", df)
        got = db.read_sql(conn, "SELECT close FROM price_daily")
    assert n == 1
    assert got.iloc[0]["close"] == 593.0


def test_fetch_log_roundtrip(tmp_path):
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    with db.connect(dbfile) as conn:
        assert db.get_last_date(conn, "price_daily", "2330") is None
        db.set_last_date(conn, "price_daily", "2330", "2024-01-05", "2024-01-06T00:00:00")
        assert db.get_last_date(conn, "price_daily", "2330") == "2024-01-05"
