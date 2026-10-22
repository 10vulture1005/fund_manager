"""Pure indicator library (pandas/numpy) — no lookahead anywhere (spec 3.6).

All functions operate on pandas Series / DataFrames indexed by candle OPEN
time, over CLOSED bars only. Higher-timeframe consumers must lag by one
completed HTF bar themselves (spec 3.6); nothing here peeks forward.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average (pandas span convention: alpha=2/(span+1))."""
    if span <= 0:
        raise ValueError("span must be positive")
    return series.astype(float).ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average over the trailing `window` closed bars."""
    if window <= 0:
        raise ValueError("window must be positive")
    return series.astype(float).rolling(window=window, min_periods=window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI on CLOSED bars, classical seeded recursion:
    the first value uses a simple mean of the first `period` moves; later
    values use Wilder's smoothing. Values before that (and NaN inputs) stay
    NaN — nothing is back-filled, so consumers see closed bars only.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    arr = series.astype(float).to_numpy(dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    if n <= period:
        return pd.Series(out, index=series.index)
    delta = np.diff(arr)  # delta[j] = arr[j+1] - arr[j]
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    avg_gain = float(up[:period].mean())
    avg_loss = float(down[:period].mean())

    def value(gain: float, loss: float) -> float:
        if loss == 0.0:
            return 100.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = value(avg_gain, avg_loss)
    for k in range(period + 1, n):
        j = k - 1
        avg_gain = (avg_gain * (period - 1) + up[j]) / period
        avg_loss = (avg_loss * (period - 1) + down[j]) / period
        out[k] = value(avg_gain, avg_loss)
    return pd.Series(out, index=series.index)


def atr(
    df: pd.DataFrame,
    period: int = 14,
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
) -> pd.Series:
    """Wilder ATR from closed bars (high/low/close), seeded like RSI: first
    value = simple mean of the first `period` true ranges (TR[0]=high-low);
    then Wilder smoothing. Earlier values stay NaN (closed bars only)."""
    if period <= 0:
        raise ValueError("period must be positive")
    high = df[high_col].astype(float).to_numpy(dtype=float)
    low = df[low_col].astype(float).to_numpy(dtype=float)
    close = df[close_col].astype(float).to_numpy(dtype=float)
    n = len(df)
    tr = np.full(n, np.nan)
    if n == 0:
        return pd.Series(tr, index=df.index)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    out = np.full(n, np.nan)
    if n < period:
        return pd.Series(out, index=df.index)
    atr_val = float(tr[:period].mean())
    out[period - 1] = atr_val
    for i in range(period, n):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        out[i] = atr_val
    return pd.Series(out, index=df.index)


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD line, signal line and histogram (12/26/9)."""
    if fast >= slow or min(fast, slow, signal) <= 0:
        raise ValueError("need 0 < fast < slow and signal > 0")
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return pd.DataFrame(
        {"macd": line, "signal": sig, "histogram": hist}, index=close.index
    )


def rolling_percentile(
    series: pd.Series, window: int, closed_only: bool = True
) -> pd.Series:
    """Percentile rank (0..1) of each close within its trailing window:
    average rank = (# values < last)/n + 0.5 × (# values == last)/n.

    Computed only once the first `window` bars have closed: earlier values
    are NaN (never back-filled — no lookahead). Constant windows score 0.5.
    """
    def avg_rank(w: np.ndarray) -> float:
        last = w[-1]
        n = len(w)
        return float((w < last).mean() + 0.5 * (w == last).mean())

    s = series.astype(float)
    out = s.rolling(window=window, min_periods=window).apply(avg_rank, raw=True)
    return out


# ------------------------------------------------------------------- pivots
def pivots(
    high: pd.Series,
    low: pd.Series,
    left: int = 3,
    right: int = 3,
) -> pd.DataFrame:
    """Fractal swing pivots, confirmed only AFTER `right` further bars close —
    i.e. a pivot at index i is EMITTED with the index of bar i but must be
    treated as known from bar i+right onward (no lookahead, spec 3.6).

    Returns a DataFrame indexed by the pivot bar time with columns:
    kind ('high'|'low'), price, confirmed_at (the index of bar i+right).
    """
    if left < 1 or right < 1:
        raise ValueError("left/right must be >= 1")
    h = high.astype(float).to_numpy()
    l = low.astype(float).to_numpy()
    n = min(len(h), len(l))
    rows: list[tuple] = []
    for i in range(left, n - right):
        window_h = h[i - left : i + right + 1]
        window_l = l[i - left : i + right + 1]
        if h[i] == window_h.max() and (window_h > h[i]).sum() == 0 and window_h.argmax() == left:
            confirmed_at = high.index[i + right]
            rows.append((high.index[i], "high", h[i], confirmed_at))
        elif l[i] == window_l.min() and (window_l < l[i]).sum() == 0 and window_l.argmin() == left:
            confirmed_at = low.index[i + right]
            rows.append((low.index[i], "low", l[i], confirmed_at))
    return pd.DataFrame(
        rows, columns=["bar", "kind", "price", "confirmed_at"]
    ).set_index("bar")


def classify_structure(swing_df: pd.DataFrame) -> str:
    """Market-structure bias from the last confirmed swings (spec 2.4).

    bullish = rising highs AND rising lows (HH+HL); bearish = LH+LL;
    anything else → 'range'.
    """
    if swing_df.empty:
        return "range"
    highs = swing_df[swing_df["kind"] == "high"]["price"].astype(float)
    lows = swing_df[swing_df["kind"] == "low"]["price"].astype(float)
    if len(highs) < 2 or len(lows) < 2:
        return "range"
    hh = highs.iloc[-1] > highs.iloc[-2]
    hl = lows.iloc[-1] > lows.iloc[-2]
    if hh and hl:
        return "bullish"
    lh = highs.iloc[-1] < highs.iloc[-2]
    ll = lows.iloc[-1] < lows.iloc[-2]
    if lh and ll:
        return "bearish"
    return "range"
