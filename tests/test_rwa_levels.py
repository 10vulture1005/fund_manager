"""Level/zone scoring on hand-built series (prompt A 2.5)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.models import Candle
from strategy.rwa_positional.levels import (
    Levels,
    atr_from_candles,
    build_levels,
    equal_high_low_clusters,
    select_zones,
    touch_count,
)

SYMBOL = "XAUTUSD"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def c(res: str, i: int, minutes: int, o: float, h: float, l: float, cl: float,
      v: float = 5.0) -> Candle:
    return Candle(symbol=SYMBOL, resolution=res,
                  ts=BASE + timedelta(minutes=minutes * i),
                  open=o, high=h, low=l, close=cl, volume=v, closed=True)


def gen_1h(n: int = 40) -> list[Candle]:
    """~1.2 wide 1H bars around 101 with two isolated dips touching 100."""
    mids: list[float] = []
    for i in range(n):
        if i in (10, 25):
            mids.append(100.2)          # single-bar dips (two touch episodes)
        else:
            mids.append(101.0)
    out = []
    for i, m in enumerate(mids):
        out.append(c("1H", i, 60, o=m - 0.1, h=m + 0.6, l=m - 0.6, cl=m + 0.15))
    return out


def gen_4h() -> list[Candle]:
    """Hand-built 4H series: swing high 112.6@10 then 115.2@22 then 115.9@31
    (rising highs), swing low 100.5@13 then 110.0@28 (rising lows) -> bullish."""
    hi = [104.2, 104.0, 104.6, 103.8, 104.4, 105.5, 108.0, 110.2, 111.4, 112.3,
          112.6, 112.5, 109.4, 104.3, 104.1, 105.3, 107.2, 111.4, 112.4, 112.9,
          114.0, 114.8, 115.2, 115.0, 114.6, 114.9, 115.1, 115.3, 115.5, 115.8,
          115.9, 116.1, 115.6, 115.7, 115.8, 115.9, 116.0, 115.8, 115.9, 115.6]
    lo = [102.9, 103.1, 102.7, 100.0, 101.1, 102.9, 104.0, 106.9, 108.4, 109.9,
          111.0, 108.5, 102.2, 100.5, 102.4, 103.1, 104.9, 106.1, 108.5, 109.5,
          110.2, 110.9, 111.4, 111.9, 112.4, 112.9, 110.5, 110.2, 110.0, 110.6,
          111.8, 112.6, 113.0, 113.4, 113.6, 113.8, 114.0, 114.2, 114.4, 114.6]
    out = []
    for i, (h, l) in enumerate(zip(hi, lo)):
        assert h > l + 0.5, (i, h, l)
        out.append(c("4H", i, 240, o=l + 0.05, h=h, l=l, cl=h - 0.2))
    return out


def test_atr_and_touches():
    one_h = gen_1h()
    atr = atr_from_candles(one_h, 14)
    assert atr is not None and 1.0 < atr < 1.4
    # level 100: two isolated dip episodes -> touches >= 2 (>= tol 0.3)
    assert touch_count(one_h, 100.0, 0.3) >= 2


def test_equal_clusters():
    pts_high = [(4, 115.0), (9, 115.0), (14, 116.0)]
    pts_low = [(5, 99.9), (12, 100.0)]
    clusters = equal_high_low_clusters(pts_high + pts_low, tol=0.2)
    # {115.0,115.0} and {99.9,100.0} cluster; 116.0 is isolated
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [2, 2]


def test_zone_score_at_least_2():
    one_h = gen_1h()
    atr = atr_from_candles(one_h, 14)
    f4h = gen_4h()
    levels = build_levels(
        candles_1h=one_h, atr_1h_value=atr,
        swings_4h=[(i, p) for i, p in _swings(f4h)],
        prev_day_high=104.4, prev_day_low=100.0,
        prev_week_high=None, prev_week_low=None,
        equal_cluster_atr_mult=0.15, zone_atr_mult=0.25,
        swing_lookback=10,
    )
    zones = select_zones(levels, atr, min_zone_score=2, zone_atr_mult=0.25)
    assert zones, f"no zones from levels {[ (lv.price, lv.score) for lv in levels ]}"
    near_100 = [z for z in zones if abs(z.price - 100.0) <= 0.3
                or abs(z.price - 100.5) <= 0.3]
    assert near_100, "expected a zone at the 100/100.5 confluence"
    best = max(near_100, key=lambda z: z.score)
    assert best.score >= 2
    # zone = level ± 0.25 x ATR(1H)
    assert abs(best.upper - (round(best.price, 10) + 0.25 * atr)) < 1e-9
    assert abs(best.lower - (round(best.price, 10) - 0.25 * atr)) < 1e-9
    # sources recorded
    assert best.sources, "zone carries its confluence sources"


def test_min_zone_score_filter_excludes_weak_levels():
    one_h = gen_1h()
    atr = atr_from_candles(one_h, 14)
    f4h = gen_4h()
    levels = build_levels(candles_1h=one_h, atr_1h_value=atr,
                          swings_4h=_swings(f4h), prev_day_high=104.4,
                          prev_day_low=100.0)
    zones = select_zones(levels, atr, min_zone_score=3, zone_atr_mult=0.25)
    assert all(z.score >= 3 for z in zones)
    high = [z for z in zones if z.price > 104.3]
    # a lone swing high at 104.x has at most 2 confluences -> excluded at 3
    assert not high, "single-source level must not pass min_zone_score=3"


def _swings(f4h: list[Candle]) -> list[tuple[int, float]]:
    import strategy.rwa_positional.levels as lv
    hi, lo = lv.pivots_from_candles(f4h, 2, 2)
    pts = [(i, p) for i, p in hi] + [(i, p) for i, p in lo]
    # confirm the structure we hand-built
    prices = sorted({round(p, 4) for _, p in hi})
    assert len(hi) >= 3 and len(lo) >= 2, (hi, lo)
    return sorted(pts)


def test_levels_facade():
    one_h = gen_1h()
    atr = atr_from_candles(one_h, 14)
    lvb = Levels({"min_zone_score": 2})
    zones = lvb.build(candles_1h=one_h, atr_1h_value=atr,
                      swings_4h=_swings(gen_4h()), prev_day_high=104.4,
                      prev_day_low=100.0)
    assert any(abs(z.price - 100.0) <= 0.35 for z in zones)
