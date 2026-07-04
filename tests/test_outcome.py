"""成果評估器單元測試（合成價格，patch 掉 DB 查詢）。"""
from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd

from src.memory import outcome


def _plan_row(action="buy", entry_high=102.0, stop=95.0, target=115.0):
    plan = {"action": action, "entry_low": 100.0, "entry_high": entry_high,
            "stop_loss": stop, "target_price": target, "confidence": 0.7,
            "rationale": "測試"}
    return {"as_of": "2024-01-01", "stock_id": "9999",
            "plan_json": json.dumps({"plan": plan, "stock_id": "9999", "analysts": {}})}


def _px(rows):
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])


def _run(row, price_df, today="2024-03-01"):
    with patch.object(outcome.q, "get_price", return_value=price_df):
        return outcome._evaluate_one(row, today)


def test_target_hit():
    px = _px([
        ["2024-01-01", 100, 101, 99, 100],
        ["2024-01-02", 101, 103, 100, 102],   # 進場日 open=101 ≤ 102
        ["2024-01-03", 103, 116, 102, 115],   # high 116 ≥ 目標 115
    ])
    r = _run(_plan_row(), px)
    assert r["outcome"] == "target_hit"
    assert abs(r["ret"] - (115 / 101 - 1)) < 1e-4


def test_stopped():
    px = _px([
        ["2024-01-01", 100, 101, 99, 100],
        ["2024-01-02", 101, 102, 100, 101],
        ["2024-01-03", 100, 100, 94, 95],     # low 94 ≤ 停損 95
    ])
    r = _run(_plan_row(), px)
    assert r["outcome"] == "stopped"
    assert abs(r["ret"] - (95 / 101 - 1)) < 1e-4


def test_same_day_both_touched_is_stopped():
    # 同日 low 觸損且 high 觸標 → 保守以停損計
    px = _px([
        ["2024-01-01", 100, 101, 99, 100],
        ["2024-01-02", 101, 120, 94, 110],
    ])
    r = _run(_plan_row(), px)
    assert r["outcome"] == "stopped"


def test_no_fill_when_gap_up():
    px = _px([
        ["2024-01-01", 100, 101, 99, 100],
        ["2024-01-02", 105, 110, 104, 108],   # 開盤 105 > entry_high 102 → 不追
    ])
    r = _run(_plan_row(), px)
    assert r["outcome"] == "no_fill" and r["ret"] == 0.0


def test_timeout_after_horizon():
    rows = [["2024-01-01", 100, 101, 99, 100],
            ["2024-01-02", 101, 102, 100, 101]]
    # 之後 horizon 天橫盤（不觸損不觸標）
    for i in range(outcome.HORIZON_DAYS):
        rows.append([f"2024-02-{i+1:02d}", 101, 103, 100, 102])
    r = _run(_plan_row(), _px(rows))
    assert r["outcome"] == "timeout"
    assert abs(r["ret"] - (102 / 101 - 1)) < 1e-4


def test_pending_when_window_not_full():
    # 只過了 3 天且未觸發 → None（之後再評）
    px = _px([
        ["2024-01-01", 100, 101, 99, 100],
        ["2024-01-02", 101, 102, 100, 101],
        ["2024-01-03", 101, 102, 100, 101],
    ])
    assert _run(_plan_row(), px) is None


def test_avoid_watched():
    rows = [["2024-01-01", 100, 101, 99, 100]]
    for i in range(outcome.HORIZON_DAYS):
        rows.append([f"2024-02-{i+1:02d}", 100 + i, 101 + i, 99 + i, 100 + i])
    r = _run(_plan_row(action="avoid", stop=None, target=None), _px(rows))
    assert r["outcome"] == "avoid_watched"
    assert r["ret"] > 0  # 避開後其實上漲 → 反思素材：避開錯了