"""資料層 P0/P1 測試：還原價、清洗、週月K 聚合（合成資料，不需連網）。"""
from __future__ import annotations

import pandas as pd

from src.data.query import _apply_adjustment, _clean_price, resample_price


def _px(rows):
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def test_adjustment_backward():
    # 除權息前 100 → 事件(before=100, after=95) → 之前的價格應乘 0.95
    df = _px([
        ["2024-01-02", 100, 101, 99, 100, 1000],
        ["2024-01-03", 95, 96, 94, 95, 1000],   # 除權息日
    ])
    div = pd.DataFrame([{"date": "2024-01-03", "before_price": 100.0, "after_price": 95.0}])
    out = _apply_adjustment(df, div)
    assert abs(out.iloc[0]["close"] - 95.0) < 1e-6      # 事件前被調整
    assert abs(out.iloc[1]["close"] - 95.0) < 1e-6      # 事件日(含)之後不變


def test_adjustment_multiple_events_compound():
    df = _px([
        ["2024-01-01", 100, 100, 100, 100, 1],
        ["2024-06-01", 100, 100, 100, 100, 1],
        ["2024-12-01", 100, 100, 100, 100, 1],
    ])
    div = pd.DataFrame([
        {"date": "2024-06-01", "before_price": 100.0, "after_price": 90.0},   # ×0.9
        {"date": "2024-12-01", "before_price": 100.0, "after_price": 80.0},   # ×0.8
    ])
    out = _apply_adjustment(df, div)
    assert abs(out.iloc[0]["close"] - 72.0) < 1e-6   # 兩事件前：0.9×0.8
    assert abs(out.iloc[1]["close"] - 80.0) < 1e-6   # 只受第二事件影響
    assert abs(out.iloc[2]["close"] - 100.0) < 1e-6  # 最新不變


def test_clean_drops_zero_and_clamps_open():
    df = _px([
        ["2024-01-02", 0, 0, 0, 0, 500],          # 全零 → 剔除
        ["2024-01-03", 78.95, 73.7, 65.1, 71.2, 100],  # open > high → 夾回
        ["2024-01-04", 0, 50, 45, 48, 100],       # open=0 → 補 low
    ])
    out = _clean_price(df)
    assert len(out) == 2
    assert abs(out.iloc[0]["open"] - 73.7) < 1e-6
    assert abs(out.iloc[1]["open"] - 45.0) < 1e-6


def test_resample_weekly():
    # 2024-01-01(一)~01-05(五) 一週
    df = _px([
        ["2024-01-01", 10, 12, 9, 11, 100],
        ["2024-01-02", 11, 15, 11, 14, 200],
        ["2024-01-05", 14, 14, 12, 13, 300],
    ])
    w = resample_price(df, "W")
    assert len(w) == 1
    r = w.iloc[0]
    assert r["open"] == 10 and r["high"] == 15 and r["low"] == 9 and r["close"] == 13
    assert r["volume"] == 600
