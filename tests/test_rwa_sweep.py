"""Sweep detector on hand-built 15m candles (prompt A 2.6)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.models import Candle
from strategy.rwa_positional.levels import Zone
from strategy.rwa_positional.sweep import filter_sweep_side, detect_sweep

SYMBOL = "XAUTUSD"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
STRIDE_MIN = 15


def candles_from(rows: list[tuple[float, float, float, float, float]],
                 res: str = "15m") -> list[Candle]:
    out = []
    for i, (o, h, l, cl, v) in enumerate(rows):
        out.append(Candle(symbol=SYMBOL, resolution=res,
                          ts=BASE + timedelta(minutes=STRIDE_MIN * i),
                          open=o, high=h, low=l, close=cl, volume=v,
                          closed=True))
    return out


def warm(n: int = 25, price: float = 101.0, v: float = 10.0) -> list[tuple]:
    """Flat 15m warmup: small wicks, no volume spikes."""
    return [(price, price + 0.15, price - 0.15, price + 0.05, v)] * n


ZONE = Zone(price=100.0, score=2, upper=100.5, lower=99.5)
ATR = 1.0


def c_so(rows, atr=ATR, **kw):
    defaults = dict(sweep_min_atr=0.10, reclaim_bars=3, volume_mult=1.2,
                    wick_frac_before=0.5, filter_direction=False,
                    volume_sma_len=20)
    defaults.update(kw)
    return detect_sweep(candles_from(rows), ZONE, atr, **defaults)


def sell_side_rows(sweep_low=94.5, sweep_vol=30.0, reclaim_close=101.0,
                   n_warm=25):
    rows = warm(n_warm)
    rows.append((100.9, 101.0, sweep_low, 100.8, sweep_vol))   # sweep bar
    rows.append((100.8, 101.2, 100.5, reclaim_close, 10.0))  # reclaim bar
    return rows


def test_sell_side_sweep_triggers_long():
    sig = c_so(sell_side_rows())
    assert sig is not None
    assert sig.kind == "sell_side"
    assert sig.direction == "long"
    assert sig.wick_extreme == 94.5
    assert sig.volume_ok and not sig.wick_ok or sig.wick_ok  # some leg must pass
    assert sig.confirm_ts > sig.sweep_ts


def test_wick_frac_passes_without_volume():
    # 15m volume SMA20 over warmup is 10 -> need 12; use volume 10 (fails)
    # but lower wick = close - low = 100.8 - 94.5 = 6.3 of range 6.5 -> > 50% -> pass
    sig = c_so(sell_side_rows(sweep_vol=10.0))
    assert sig is not None and sig.wick_ok and not sig.volume_ok


def test_volume_or_wick_requirement_blocks():
    # volume 10 < 12 AND wick 5% of range -> not a valid sweep (close sits on
    # the low, so the "lower wick" is ~0)
    rows = warm() + [(99.9, 100.0, 98.0, 98.1, 10.0),
                     (98.1, 100.6, 98.0, 100.5, 10.0)]
    assert c_so(rows) is None


def test_no_reclaim_within_3_bars_blocks():
    rows = warm() + [(100.9, 101.0, 94.5, 100.8, 30.0),
                     (100.8, 100.9, 99.0, 99.2, 10.0),   # still under level
                     (99.0, 99.4, 98.8, 99.1, 10.0),
                     (99.1, 99.5, 99.0, 99.3, 10.0)]     # 3 bars, no reclaim
    assert c_so(rows) is None


def test_reclaim_on_third_bar_confirms():
    rows = warm() + [(100.9, 101.0, 94.5, 100.8, 30.0),
                     (100.8, 100.9, 99.5, 99.8, 10.0),
                     (99.8, 100.0, 99.6, 99.9, 10.0),
                     (99.9, 100.1, 99.7, 100.05, 10.0)]  # close > 100 on bar 3
    sig = c_so(rows)
    assert sig is not None and sig.kind == "sell_side"


def test_depth_requirement_blocks_shallow_wick():
    # wick 3cm shallower than 0.10 x ATR below the zone low: 99.45 vs 99.4
    rows = warm() + [(99.9, 100.0, 99.45, 100.4, 30.0),
                     (100.4, 100.6, 100.2, 100.55, 10.0)]
    assert c_so(rows) is None


def test_buy_side_sweep_mirror_short():
    zone_short = Zone(price=100.0, score=2, upper=100.5, lower=99.5)
    rows = [(99.0, 99.15, 98.85, 99.05, 10.0)] * 10
    rows.append((99.1, 105.5, 99.1, 99.2, 30.0))       # buy-side sweep high
    rows.append((99.2, 99.4, 99.0, 99.05, 10.0))       # close back under level
    sig = detect_sweep(candles_from(rows), zone_short, ATR,
                       sweep_min_atr=0.10, reclaim_bars=3, volume_mult=1.2,
                       wick_frac_before=0.5, filter_direction=False)
    assert sig is not None
    assert sig.kind == "buy_side" and sig.wick_extreme == 105.5


def test_direction_filter_blocks_counter_trend_by_default():
    # sell-side sweep wants a LONG but 4H bias is bearish -> blocked
    assert filter_sweep_side(wanted_direction="long", bias="bearish",
                             zone_price=100.0, range_mid=None,
                             zone_score=2) is None
    # same sweep with a bullish bias -> allowed
    assert filter_sweep_side(wanted_direction="long", bias="bullish",
                             zone_price=100.0, range_mid=None,
                             zone_score=2) == "long"


def test_direction_filter_range_mid_rule():
    # range: longs only when the zone sits at/below the range mid
    assert filter_sweep_side(wanted_direction="long", bias="range",
                             zone_price=100.0, range_mid=108.0,
                             zone_score=2) == "long"
    assert filter_sweep_side(wanted_direction="long", bias="range",
                             zone_price=120.0, range_mid=108.0,
                             zone_score=2) is None
    assert filter_sweep_side(wanted_direction="short", bias="range",
                             zone_price=120.0, range_mid=108.0,
                             zone_score=2) == "short"


def test_counter_trend_needs_flag_and_score_3():
    assert filter_sweep_side(wanted_direction="long", bias="bearish",
                             zone_price=100.0, range_mid=None, zone_score=3,
                             allow_countertrend=True) == "long"
    assert filter_sweep_side(wanted_direction="long", bias="bearish",
                             zone_price=100.0, range_mid=None, zone_score=2,
                             allow_countertrend=True) is None


def test_detect_blocked_by_direction_filter():
    # valid sell-side sweep but bearish bias + default config -> no signal
    sig = c_so(sell_side_rows(), filter_direction=True, bias="bearish")
    assert sig is None
    sig = c_so(sell_side_rows(), filter_direction=True, bias="bullish")
    assert sig is not None and sig.direction == "long"
