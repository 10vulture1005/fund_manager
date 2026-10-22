"""4H market-structure bias, BOS / CHoCH (spec 2.4).

Pure functions over closed-candle lists.

- Bullish: last two confirmed swing highs AND swing lows are rising (HH+HL).
- Bearish: falling (LH+LL).
- Otherwise `range`; tiebreak (config `use_ema_tiebreak`): latest 4H close
  above EMA200(4H) leans long (`range_long`), below leans short
  (`range_short`).
- BOS = 4H close beyond the last swing in the trend direction.
- CHoCH = 4H close beyond the last OPPOSITE swing -> bias flips.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import pandas as pd

from core.models import Candle
from strategy.rwa_positional.levels import pivots_from_candles


@dataclass
class Structure:
    bias: str = "range"          # bullish | bearish | range_long | range_short | range
    last_high: Optional[float] = None   # most recent confirmed swing high
    prev_high: Optional[float] = None
    last_low: Optional[float] = None    # most recent confirmed swing low
    prev_low: Optional[float] = None
    range_mid: Optional[float] = None
    bos: Optional[str] = None    # "bull" | "bear" — latest BOS event
    choch: Optional[str] = None  # "bull" | "bear" — latest CHoCH event
    event: Optional[str] = None  # last event name, e.g. "bos_bull"
    ema200: Optional[float] = None
    features: dict = field(default_factory=dict)

    def features_dict(self) -> dict:
        return {
            "bias": self.bias,
            "last_high": self.last_high,
            "prev_high": self.prev_high,
            "last_low": self.last_low,
            "prev_low": self.prev_low,
            "range_mid": self.range_mid,
            "bos": self.bos,
            "choch": self.choch,
            "ema200": self.ema200,
        }


def compute_ema200_4h(candles: Sequence[Candle]) -> Optional[float]:
    if len(candles) < 200:
        return None
    closes = pd.Series([float(c.close) for c in candles])
    v = closes.ewm(span=200, adjust=False).mean().iloc[-1]
    return None if pd.isna(v) else float(v)


def compute_structure(
    candles_4h: Sequence[Candle],
    left: int = 2,
    right: int = 2,
    use_ema_tiebreak: bool = True,
) -> Structure:
    """Compute structure state from CLOSED 4H candles."""
    st = Structure()
    if len(candles_4h) < left + right + 2:
        return st

    swing_highs, swing_lows = pivots_from_candles(candles_4h, left, right)
    st.ema200 = compute_ema200_4h(candles_4h)

    if swing_highs:
        st.last_high = swing_highs[-1][1]
        st.prev_high = swing_highs[-2][1] if len(swing_highs) >= 2 else None
    if swing_lows:
        st.last_low = swing_lows[-1][1]
        st.prev_low = swing_lows[-2][1] if len(swing_lows) >= 2 else None

    # raw bias from swing structure
    raw_bias = "range"
    if st.last_high is not None and st.last_low is not None:
        hh = st.prev_high is not None and st.last_high > st.prev_high
        hl = st.prev_low is not None and st.last_low > st.prev_low
        lh = st.prev_high is not None and st.last_high < st.prev_high
        ll = st.prev_low is not None and st.last_low < st.prev_low
        if hh and hl:
            raw_bias = "bullish"
        elif lh and ll:
            raw_bias = "bearish"
    st.range_mid = (
        (st.last_high + st.last_low) / 2.0
        if st.last_high is not None and st.last_low is not None
        else None
    )

    last_close = float(candles_4h[-1].close)
    # BOS / CHoCH against the last opposite swing
    if st.last_high is not None and last_close > st.last_high:
        if raw_bias == "bullish":
            st.bos, st.event = "bull", "bos_bull"
        else:
            st.choch, st.event = "bull", "choch_bull"
            raw_bias = "bullish"  # bias flip
    elif st.last_low is not None and last_close < st.last_low:
        if raw_bias == "bearish":
            st.bos, st.event = "bear", "bos_bear"
        else:
            st.choch, st.event = "bear", "choch_bear"
            raw_bias = "bearish"  # bias flip

    # range tiebreak with EMA200
    if raw_bias == "range" and use_ema_tiebreak and st.ema200 is not None:
        raw_bias = "range_long" if last_close > st.ema200 else "range_short"

    st.bias = raw_bias
    st.features = st.features_dict()
    return st


def choch_against(
    candles_4h: Sequence[Candle],
    side: str,  # "long" | "short"
    left: int = 2,
    right: int = 2,
) -> bool:
    """True when the latest closed 4H candle made a CHoCH against `side`:
    long -> close below the last confirmed swing low;
    short -> close above the last confirmed swing high."""
    if len(candles_4h) < left + right + 2:
        return False
    swing_highs, swing_lows = pivots_from_candles(candles_4h, left, right)
    close = float(candles_4h[-1].close)
    if side == "long":
        return bool(swing_lows and close < swing_lows[-1][1])
    else:
        return bool(swing_highs and close > swing_highs[-1][1])


def structure_exit(
    candles_4h: Sequence[Candle], zone, side: str, left: int = 2, right: int = 2
) -> bool:
    """Structure exit (spec 2.8): a 4H close beyond the zone TOGETHER WITH a
    CHoCH against the position -> close the whole campaign."""
    if not candles_4h or zone is None:
        return False
    # accept either a zone object (.lower/.upper) or a Campaign
    # (.zone_lower/.zone_upper)
    lo = getattr(zone, "zone_lower", None)
    if lo is None:
        lo = getattr(zone, "lower", None)
    hi = getattr(zone, "zone_upper", None)
    if hi is None:
        hi = getattr(zone, "upper", None)
    if lo is None or hi is None:
        return False
    close = float(candles_4h[-1].close)
    if side == "long":
        beyond = close < lo
        flipped = choch_against(candles_4h, "long", left, right)
    else:
        beyond = close > hi
        flipped = choch_against(candles_4h, "short", left, right)
    return bool(beyond and flipped)
