"""Indicator tests — synthetic series, independent re-implementations
inline per spec 3.7 (EMA/RSI/ATR/MACD + pivot detector on hand-built zigzags).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators import (
    atr,
    classify_structure,
    ema,
    macd,
    pivots,
    rolling_percentile,
    rsi,
    sma,
)


def idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")


class TestEma:
    def test_matches_independent_recursive_formula(self):
        s = pd.Series([10.0, 11, 12.5, 13, 15, 14.5, 16], index=idx(7))
        span = 4
        alpha = 2.0 / (span + 1.0)
        expected = [0.0] * 7
        expected[0] = s.iloc[0]
        for i in range(1, len(s)):
            expected[i] = alpha * s.iloc[i] + (1 - alpha) * expected[i - 1]
        out = ema(s, span)
        assert np.allclose(out.to_numpy(), np.array(expected), atol=1e-9)

    def test_constant_series_converges(self):
        s = pd.Series([5.0] * 50, index=idx(50))
        assert abs(ema(s, 20).iloc[-1] - 5.0) < 1e-9


class TestSma:
    def test_mean_of_window(self):
        s = pd.Series(np.arange(10, dtype=float), index=idx(10))
        out = sma(s, 4)
        assert abs(out.iloc[3] - 1.5) < 1e-9
        assert abs(out.iloc[9] - 7.5) < 1e-9
        assert np.isnan(out.iloc[2])  # needs a full window

    def test_no_lookahead(self):
        # changing a FUTURE value must not change earlier outputs
        s1 = pd.Series(np.arange(10, dtype=float), index=idx(10))
        s2 = s1.copy()
        s2.iloc[-1] = 999.0
        out1, out2 = sma(s1, 3), sma(s2, 3)
        assert np.allclose(out1.iloc[:-1].to_numpy(), out2.iloc[:-1].to_numpy(),
                           equal_nan=True)


class TestRsi:
    def test_matches_independent_wilder_loop(self):
        close = pd.Series(
            [44.34, 44.30, 44.36, 43.61, 44.33, 44.83, 45.10, 45.42,
             45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
             46.03, 46.41, 46.34, 46.16],
            index=idx(20),
        )
        period = 14
        delta = np.diff(close.to_numpy())
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        # seed: simple mean of the first `period` moves → RSI at bar index `period`
        avg_gain = gains[:period].mean()
        avg_loss = losses[:period].mean()

        def val(g: float, l: float) -> float:
            return 100.0 - 100.0 / (1.0 + g / l) if l else 100.0

        expected_values = [val(avg_gain, avg_loss)]
        for i in range(period, len(delta)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            expected_values.append(val(avg_gain, avg_loss))
        out = rsi(close, period)
        got = out.iloc[period:].to_numpy()
        assert np.allclose(got, np.array(expected_values), atol=1e-9)

    def test_bounded(self):
        rng = np.random.default_rng(7)
        s = pd.Series(np.cumsum(rng.normal(size=80)), index=idx(80))
        out = rsi(s, 14)
        non_na = out.dropna()
        assert ((non_na >= 0) & (non_na <= 100)).all()
        assert out.iloc[:13].isna().all()  # warm-up is NaN (closed bars only)


class TestAtr:
    def test_matches_independent_wilder_loop(self):
        rng = np.random.default_rng(3)
        high = pd.Series(100 + rng.uniform(0, 2, size=40), index=idx(40))
        low = pd.Series(98 + rng.uniform(0, 2, size=40), index=idx(40))
        close = pd.Series(99 + rng.uniform(0, 2, size=40), index=idx(40))
        df = pd.DataFrame({"high": high, "low": low, "close": close})
        period = 14
        tr = np.zeros(len(df))
        tr[0] = high.iloc[0] - low.iloc[0]
        for i in range(1, len(df)):
            tr[i] = max(
                high.iloc[i] - low.iloc[i],
                abs(high.iloc[i] - close.iloc[i - 1]),
                abs(low.iloc[i] - close.iloc[i - 1]),
            )
        expected = np.zeros(len(df))
        expected[period - 1] = tr[:period].mean()
        for i in range(period, len(df)):
            expected[i] = (expected[i - 1] * (period - 1) + tr[i]) / period
        out = atr(df, period)
        got = out.iloc[period - 1:].to_numpy()
        assert np.allclose(got, expected[period - 1:], atol=1e-9)


class TestMacd:
    def test_histogram_is_line_minus_signal(self):
        close = pd.Series(np.cumsum(np.sin(np.arange(60) * 0.3)) + 100.0, index=idx(60))
        out = macd(close, 12, 26, 9)
        assert np.allclose(
            out["histogram"].to_numpy(),
            (out["macd"] - out["signal"]).to_numpy(),
            atol=1e-12,
        )
        # independent recompute of the line: ema12 - ema26
        line = ema(close, 12) - ema(close, 26)
        assert np.allclose(out["macd"].to_numpy(), line.to_numpy(), atol=1e-12)


class TestRollingPercentile:
    def test_bounded_and_monotone_examples(self):
        s = pd.Series([1, 2, 3, 4, 5, 5.5, 3.0], index=idx(7))
        out = rolling_percentile(s, 5)
        assert out.iloc[:4].isna().all()  # incomplete windows → NaN
        non_na = out.dropna()
        assert ((non_na >= 0) & (non_na <= 1)).all()
        assert out.iloc[5] == pytest.approx(0.9)
        # …and the following 3.0 close ranks BELOW mid (broken run)
        assert out.iloc[6] < out.iloc[5]

    def test_constant_series_mid_rank(self):
        s = pd.Series([7.0] * 20, index=idx(20))
        out = rolling_percentile(s, 10)
        assert abs(out.iloc[-1] - 0.5) < 1e-9


class TestPivots:
    @staticmethod
    def zigzag(vals: list[float], bars: int = 4) -> pd.DataFrame:
        """Hand-built zigzag from alternating swing levels: strictly monotone
        interpolated legs (no equal-value ties), so every interior swing point
        is a strict fractal pivot. `bars` bars per leg."""
        highs, lows = [], []
        for i in range(len(vals) - 1):
            a, b = vals[i], vals[i + 1]
            step = abs(b - a) / bars
            depth = step / 2.0
            for j in range(1, bars + 1):  # exclude the shared endpoint value a
                v = a + (b - a) * j / bars
                if b > a:  # ascending leg: ends ON the peak b
                    highs.append(v)
                    lows.append(v - depth)
                else:      # descending leg: ends ON the trough b
                    highs.append(v + depth)
                    lows.append(v)
        n = len(highs)
        data = pd.DataFrame({"high": highs, "low": lows}, index=idx(n))
        data["close"] = data["high"]
        data["open"] = data["low"]
        return data

    def test_highs_lows_detection_leftright3(self):
        # rising zigzag: HH + HL (both swings rising)
        df = self.zigzag([9, 11, 10, 13, 11, 15, 12])
        sw = pivots(df["high"], df["low"], left=3, right=3)
        highs = sw[sw["kind"] == "high"]["price"].to_numpy()
        lows = sw[sw["kind"] == "low"]["price"].to_numpy()
        assert len(highs) >= 2 and len(lows) >= 2
        assert (np.diff(highs) > 0).all(), "swing highs must rise (HH)"
        assert (np.diff(lows) > 0).all(), "swing lows must rise (HL)"
        assert classify_structure(sw) == "bullish"

    def test_confirmation_lag_no_lookahead(self):
        left = right = 3
        df = self.zigzag([9, 11, 10, 13, 11, 15, 12])
        sw = pivots(df["high"], df["low"], left=left, right=right)
        for bar, row in sw.iterrows():
            pos = df.index.get_loc(bar)
            assert df.index[pos + right] == row["confirmed_at"]
        # a pivot in the final `right` bars can never be CONFIRMED, so the
        # last emitted pivot bar must sit at least `right` bars before the end
        assert df.index.get_loc(sw.index[-1]) < len(df) - right

    def test_falling_structure_bearish(self):
        # falling zigzag: LH + LL (both swings falling)
        df = self.zigzag([12, 10, 11, 8, 10, 6, 9])
        sw = pivots(df["high"], df["low"], left=3, right=3)
        highs = sw[sw["kind"] == "high"]["price"].to_numpy()
        lows = sw[sw["kind"] == "low"]["price"].to_numpy()
        assert len(highs) >= 2 and len(lows) >= 2
        assert (np.diff(highs) < 0).all(), "swing highs must fall (LH)"
        assert (np.diff(lows) < 0).all(), "swing lows must fall (LL)"
        assert classify_structure(sw) == "bearish"

    def test_flat_series_yields_no_pivots(self):
        flat = pd.Series([50.0] * 30, index=idx(30))
        sw = pivots(flat, flat - 1, left=3, right=3)
        assert sw.empty
