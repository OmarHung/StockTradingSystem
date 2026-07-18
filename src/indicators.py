"""技術指標計算（純函式，pandas 實作，無外部 TA 相依）。

輸入皆為已依日期升冪排序的 price DataFrame，欄位：open/high/low/close/volume。
回傳新增指標欄位的副本，不修改原輸入。供 Screener、回測、技術分析師共用。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=window).mean()


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "macd_signal": signal_line, "macd_hist": hist})


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Wilder：一段期間內從無下跌（avg_loss==0 且有上漲）時 RSI=100，而非 NaN——
    # 否則剛上市即連漲的短歷史股會拿到缺值而非超買訊號。warmup 前 avg_loss 為
    # NaN，(avg_loss==0) 為 False，仍維持 NaN。
    return out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9,
        k_smooth: int = 3, d_smooth: int = 3) -> pd.DataFrame:
    """台股常用 KD（隨機指標）。回傳 k、d 欄位。"""
    lowest = low.rolling(n, min_periods=n).min()
    highest = high.rolling(n, min_periods=n).max()
    rsv = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False, min_periods=1).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False, min_periods=1).mean()
    return pd.DataFrame({"k": k, "d": d})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """平均真實區間（供停損距離、波動度衡量）。"""
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """平均趨向指標 ADX（衡量趨勢強度，>25 常視為有明顯趨勢）。"""
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = pd.concat(
        [(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1
    ).max(axis=1)
    atr_ = tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, adjust=False, min_periods=window).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, adjust=False, min_periods=window).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, window)
    std = close.rolling(window, min_periods=window).std()
    return pd.DataFrame({
        "bb_mid": mid,
        "bb_upper": mid + num_std * std,
        "bb_lower": mid - num_std * std,
    })


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """一次補齊常用指標欄位（供回測與分析師）。df 需含 open/high/low/close/volume。"""
    if df.empty:
        return df
    out = df.copy()
    close, high, low, vol = out["close"], out["high"], out["low"], out["volume"]

    out["ma5"] = sma(close, 5)
    out["ma20"] = sma(close, 20)
    out["ma60"] = sma(close, 60)
    out["vol_ma5"] = sma(vol, 5)
    out["vol_ma20"] = sma(vol, 20)
    out = pd.concat([out, macd(close)], axis=1)
    out["rsi14"] = rsi(close, 14)
    out = pd.concat([out, kdj(high, low, close)], axis=1)
    out["atr14"] = atr(high, low, close, 14)
    out = pd.concat([out, bollinger(close)], axis=1)

    # 報酬率（供動能因子/回測）
    out["ret_1d"] = close.pct_change()
    return out
