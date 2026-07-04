"""FinMind 402 自動等待重試測試（mock HTTP 與 sleep，不連網不真等）。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.data.finmind_client import FinMindClient, FinMindQuotaExhausted


def _resp(status_code, payload=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload or {"status": 200, "data": [{"stock_id": "2330", "date": "2026-07-03"}]}
    r.raise_for_status.return_value = None
    return r


def test_no_wait_mode_raises_immediately():
    c = FinMindClient(base_url="http://x", quota_wait=False)
    with patch.object(c._session, "get", return_value=_resp(402)):
        with pytest.raises(FinMindQuotaExhausted):
            c.get_dataset("TaiwanStockPrice", data_id="2330")


def test_wait_mode_retries_after_quota_reset():
    waits = []
    c = FinMindClient(base_url="http://x", quota_wait=True,
                      on_quota_wait=lambda t: waits.append(t))
    # 前兩次 402，第三次成功
    responses = [_resp(402), _resp(402), _resp(200)]
    with patch.object(c._session, "get", side_effect=responses), \
         patch("src.data.finmind_client.time.sleep") as fake_sleep:
        df = c.get_dataset("TaiwanStockPrice", data_id="2330")
    assert len(df) == 1                      # 最終拿到資料
    assert len(waits) == 2                   # 通知了兩次等待（含恢復時間）
    assert fake_sleep.call_count >= 2        # 確實睡了（被 mock 掉）
    assert all(":" in t for t in waits)      # 恢復時間為時間字串


def test_wait_mode_gives_up_after_max_waits():
    c = FinMindClient(base_url="http://x", quota_wait=True, max_quota_waits=3)
    with patch.object(c._session, "get", return_value=_resp(402)), \
         patch("src.data.finmind_client.time.sleep"):
        with pytest.raises(FinMindQuotaExhausted, match="放棄"):
            c.get_dataset("TaiwanStockPrice", data_id="2330")