"""news scout 驗證層單元測試（不需連網、不呼叫 LLM）。

重點案例：LLM 幻覺代號（代號查得到但公司名對不上）必須整檔剔除，
不能照代號反查名稱把幻覺洗白成合法候選。
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.agents import scout
from src.agents.scout import ScoutCandidate, _name_matches, _validate
from src.data import database as db


def test_name_matches_loose():
    assert _name_matches("漢翔", "漢翔航空")           # 簡稱 vs 全名
    assert _name_matches("台積電", "台積電")
    assert _name_matches("貿聯-KY", "貿聯")            # -KY 尾綴
    assert _name_matches("", "任何名稱")               # 池內無名稱 → 無從比對，放行
    assert not _name_matches("益張", "日友環保")       # 幻覺代號掛錯名


@pytest.fixture
def scout_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "t.db"
    db.init_db(dbfile)
    info = pd.DataFrame([
        {"stock_id": "2634", "stock_name": "漢翔", "type": "twse"},
        {"stock_id": "8342", "stock_name": "益張", "type": "tpex"},
        {"stock_id": "6753", "stock_name": "龍德造船", "type": "tpex"},
    ])
    prices = pd.DataFrame([
        {"stock_id": sid, "date": f"2026-{m:02d}-{d:02d}", "close": 100.0}
        for sid in ("2634", "8342", "6753")
        for m in range(1, 5) for d in range(1, 21)  # 每檔 80 天 > 60 門檻
    ])
    with db.connect(dbfile) as conn:
        db.upsert_dataframe(conn, "stock_info", info)
        db.upsert_dataframe(conn, "price_daily", prices)

    class _Cfg:
        db_path = dbfile

    monkeypatch.setattr(scout, "get_settings", lambda: _Cfg())
    return dbfile


def test_validate_rejects_hallucinated_id(scout_db):
    cands = [
        ScoutCandidate(stock_id="2634", name="漢翔航空", theme="國防", reason="軍工漲停潮報導"),
        # 8342 在池內是益張，LLM 卻說是日友環保 → 代號幻覺，須剔除
        ScoutCandidate(stock_id="8342", name="日友環保", theme="國防", reason="長榮航太相關報導"),
        ScoutCandidate(stock_id="6753", name="龍德造船", theme="國防", reason="國艦國造報導"),
    ]
    out = _validate(cands, "2026-07-05", max_c=3)
    assert [c["stock_id"] for c in out] == ["2634", "6753"]
    assert out[0]["name"] == "漢翔"  # 名稱一致時仍以股票池為準


def test_validate_dedups_and_rejects_uncertain(scout_db):
    cands = [
        ScoutCandidate(stock_id="2634", name="漢翔", theme="國防", reason="軍工報導"),
        ScoutCandidate(stock_id="2634", name="漢翔", theme="航太", reason="重複的同一檔"),
        ScoutCandidate(stock_id="8342", name="益張", theme="國防", reason="代號需確認，暫列於此"),
        ScoutCandidate(stock_id="6753", name="龍德造船", theme="國防", reason="國艦國造報導"),
    ]
    out = _validate(cands, "2026-07-05", max_c=3)
    assert [c["stock_id"] for c in out] == ["2634", "6753"]
