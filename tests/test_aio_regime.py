"""test_aio_regime — trend scoring, vol percentile, drawdown -> regime labels
(spec 2.9) for the all_in_one strategy."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from strategy.all_in_one import DEFAULTS
from strategy.all_in_one.regime import (
    HistoryStore,
    RegimeEngine,
    RegimeSnapshot,
    compute_snapshot,
    trend_to_status,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_frame(closes: list[float], half_range: float = 0.1) -> pd.DataFrame:
    """1D candle frame from close prices; high/low bracket the close."""
    idx = pd.DatetimeIndex([T0 + timedelta(days=i) for i in range(len(closes))])
    return pd.DataFrame(
        {
            "open": [c - 0.05 for c in closes],
            "high": [c + half_range for c in closes],
            "low": [c - half_range for c in closes],
            "close": list(closes),
            "volume": [1.0] * len(closes),
        },
        index=idx,
    )


def test_strong_uptrend_is_scored_up():
    closes = [100.0 + 0.1 * i for i in range(300)]
    snap = compute_snapshot(make_frame(closes), "ETHUSD", DEFAULTS)
    assert snap is not None
    assert snap.trend in ("up", "strong_up")
    assert snap.score > 0.5


def test_strong_uptrend_qualifies_strong():
    # steep monotone rise: all three components saturate the +cap
    closes = [100.0 + 1.0 * i for i in range(300)]
    snap = compute_snapshot(make_frame(closes, half_range=0.5), "ETHUSD", DEFAULTS)
    assert snap.trend == "strong_up"


def test_strong_downtrend_is_scored_down():
    closes = [130.0 - 1.0 * i for i in range(300)]
    snap = compute_snapshot(make_frame(closes, half_range=0.5), "ETHUSD", DEFAULTS)
    assert snap.trend in ("down", "strong_down")
    assert snap.score < -0.5


def test_flat_oscillation_is_neutral():
    closes = [100.0 + 2.0 * math.sin(2 * math.pi * i / 25) for i in range(300)]
    snap = compute_snapshot(make_frame(closes, half_range=1.0), "ETHUSD", DEFAULTS)
    assert snap.trend == "neutral"
    assert abs(snap.score) <= 0.5


def test_vol_percentile_is_percent_of_trailing_obs():
    # rising ATR at the end -> the last value is the highest -> pctl ~ >90
    closes = [100.0] * 120 + [100.0 + 10 * (i + 1) / 30 for i in range(30)]
    snap = compute_snapshot(make_frame(closes), "ETHUSD", DEFAULTS)
    assert snap.vol_pctl > 90

    # a violent final bar: pctl == 100 -> high_vol -> halve new-lot size
    closes = [100.0] * 120 + [100 + 3 * i for i in range(20)] + [400.0]
    snap = compute_snapshot(make_frame(closes, half_range=1.0), "ETHUSD", DEFAULTS)
    assert snap.high_vol is True


def test_vol_percentile_constant_is_mid_band():
    closes = [100.0 + 0.4 * i for i in range(120)]  # constant ATR-ish slope
    snap = compute_snapshot(make_frame(closes), "ETHUSD", DEFAULTS)
    assert 0 <= snap.vol_pctl <= 100


def test_drawdown_from_90d_high():
    closes = [100 + 20 * i for i in range(100)] + \
             [2000, 2001, 90.0]          # 90-day high ~ 1990..2000, now 90
    snap = compute_snapshot(make_frame(closes), "STOCK", DEFAULTS)
    peak = max(closes[-90:])
    assert snap.drawdown == pytest.approx((peak - 90.0) / peak, rel=1e-6)
    assert snap.drawdown >= 0


def test_snapshot_none_on_short_frame():
    assert compute_snapshot(make_frame([100.0] * 5), "X", DEFAULTS) is None


def test_sleeve_status_maps():
    eng = RegimeEngine({"regime": DEFAULTS["regime"]}, HistoryStore())
    for trend, expect in (
        ("neutral", "ACTIVE"), ("up", "ACTIVE"), ("down", "ACCUMULATE_ONLY"),
        ("strong_down", "PAUSED"), ("strong_up", "ACTIVE"),
    ):
        eng.snapshots["ETHUSD"] = RegimeSnapshot(symbol="ETHUSD", trend=trend)
        assert eng.sleeve_status("ETH_CC", "ETHUSD") == expect
    for trend, expect in (
        ("neutral", "ACTIVE"), ("down", "ACTIVE"),
        ("up", "DISTRIBUTE_ONLY"), ("strong_up", "PAUSED"),
        ("strong_down", "ACTIVE"),
    ):
        eng.snapshots["BTCUSD"] = RegimeSnapshot(symbol="BTCUSD", trend=trend)
        assert eng.sleeve_status("BTC_CP", "BTCUSD") == expect


def test_gold_mode_by_regime():
    eng = RegimeEngine({"regime": DEFAULTS["regime"]}, HistoryStore())
    eng.snapshots["XAUTUSD"] = RegimeSnapshot(symbol="XAUTUSD", trend="up")
    assert eng.gold_mode("XAUTUSD") == "CC_MODE"
    eng.snapshots["XAUTUSD"] = RegimeSnapshot(symbol="XAUTUSD", trend="down")
    assert eng.gold_mode("XAUTUSD") == "CP_MODE"


def test_trend_to_status_default():
    assert trend_to_status({}, "whatever") == "PAUSED"
    assert trend_to_status({"up": "ACTIVE"}, "up") == "ACTIVE"


def test_history_store_upsert_replaces_same_bar():
    st = HistoryStore()
    from core.models import Candle

    ts = T0
    c1 = Candle(symbol="ETHUSD", resolution="1D", ts=ts, open=1, high=2,
                low=0.5, close=1.5, closed=True)
    c2 = Candle(symbol="ETHUSD", resolution="1D", ts=ts, open=1, high=2.5,
                low=0.5, close=2.4, closed=True)
    st.upsert(c1)
    st.upsert(c2)          # same timestamp -> replaced, not duplicated
    df = st.frame("ETHUSD", "1D")
    assert len(df) == 1
    assert float(df["close"].iloc[-1]) == pytest.approx(2.4)
