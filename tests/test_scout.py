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
    assert _name_matches("士電", "士林電機")           # 跳字簡稱（子序列）
    assert _name_matches("中鋼", "中國鋼鐵")
    assert not _name_matches("益張", "日友環保")       # 幻覺代號掛錯名
    assert not _name_matches("士電", "台達電")         # 子序列不成立（士不在其中）


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

        def get(self, key, default=None):
            if key == "news":
                return {"scout": {"enabled": True, "max_candidates": 2, "source": "rss"}}
            return default

    monkeypatch.setattr(scout, "get_settings", lambda: _Cfg())
    return dbfile


def test_validate_rejects_hallucinated_id(scout_db):
    cands = [
        ScoutCandidate(stock_id="2634", name="漢翔航空", theme="國防", reason="軍工漲停潮報導"),
        # 8342 在池內是益張，LLM 卻說是日友環保 → 代號幻覺，須剔除
        ScoutCandidate(stock_id="8342", name="日友環保", theme="國防", reason="長榮航太相關報導"),
        ScoutCandidate(stock_id="6753", name="龍德造船", theme="國防", reason="國艦國造報導"),
    ]
    out, rejects = _validate(cands, "2026-07-05", max_c=3)
    assert [c["stock_id"] for c in out] == ["2634", "6753"]
    assert out[0]["name"] == "漢翔"  # 名稱一致時仍以股票池為準
    assert len(rejects) == 1 and "8342" in rejects[0] and "益張" in rejects[0]


def test_validate_rejects_pair_conflict_in_reason(scout_db):
    cands = [
        # reason 提到的 8342 在池內是益張，卻掛「長榮航太」→ 幻覺污染，整檔剔除
        ScoutCandidate(stock_id="2634", name="漢翔", theme="國防",
                       reason="國防預算擴編，漢翔（2634）與長榮航太（8342）同列受惠"),
        # reason 內的配對與池一致（含全形/半形括號、前綴雜訊）→ 保留
        ScoutCandidate(stock_id="6753", name="龍德造船", theme="國艦國造",
                       reason="受惠股龍德造船(6753)獲海巡署訂單"),
        # reason 提到池外代號 → 無從比對，不因此剔除
        ScoutCandidate(stock_id="8342", name="益張", theme="國防",
                       reason="與中信造船（2644）同題材"),
    ]
    out, rejects = _validate(cands, "2026-07-05", max_c=3)
    assert [c["stock_id"] for c in out] == ["6753", "8342"]
    assert len(rejects) == 1


def test_validate_dedups_and_rejects_uncertain(scout_db):
    cands = [
        ScoutCandidate(stock_id="2634", name="漢翔", theme="國防", reason="軍工報導"),
        ScoutCandidate(stock_id="2634", name="漢翔", theme="航太", reason="重複的同一檔"),
        ScoutCandidate(stock_id="8342", name="益張", theme="國防", reason="代號需確認，暫列於此"),
        ScoutCandidate(stock_id="6753", name="龍德造船", theme="國防", reason="國艦國造報導"),
    ]
    out, rejects = _validate(cands, "2026-07-05", max_c=3)
    assert [c["stock_id"] for c in out] == ["2634", "6753"]
    assert len(rejects) == 1  # 重複代號不列入回饋（非幻覺），只有自述不確定那檔


def test_run_news_scout_retries_on_rejects(scout_db, monkeypatch):
    """驗證剔除後帶原因重跑：第一輪一檔幻覺被剔，第二輪修正補齊即停。"""
    from src.agents.scout import ScoutReport

    reports = [
        ScoutReport(candidates=[
            ScoutCandidate(stock_id="2634", name="漢翔", theme="國防", reason="軍工報導"),
            # 幻覺：8342 池內是益張
            ScoutCandidate(stock_id="8342", name="日友環保", theme="國防", reason="航太報導"),
        ], summary="第一輪總結"),
        ScoutReport(candidates=[
            ScoutCandidate(stock_id="6753", name="龍德造船", theme="國防", reason="國艦國造報導"),
        ], summary="修正輪總結"),
    ]
    prompts: list[str] = []

    def fake_call(model, system, user_prompt, schema, **kw):
        prompts.append(user_prompt)
        return reports[len(prompts) - 1]

    monkeypatch.setattr(scout.llm, "call_structured", fake_call)
    monkeypatch.setattr(scout.llm, "log_note", lambda *a, **k: None)
    monkeypatch.setattr(scout, "_rss_notes", lambda scfg: ("素材文字", []))
    monkeypatch.setattr(scout, "_save_snapshot", lambda *a, **k: None)

    out = scout.run_news_scout("2026-07-05")
    assert [c["stock_id"] for c in out] == ["2634", "6753"]
    assert len(prompts) == 2  # 一輪初跑 + 一輪修正即收斂
    assert "驗證回饋" in prompts[1] and "8342" in prompts[1] and "益張" in prompts[1]
    assert "2634" in prompts[1]  # 已通過的檔會告知不必重列
