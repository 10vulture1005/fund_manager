"""Liquidity-sweep trigger on closed 15m candles (spec 2.6).

SELL-SIDE sweep -> LONG setup:
  a candle wicks below the zone's low by >= sweep_min_atr x ATR(1H), price
  closes back ABOVE the level within reclaim_bars, AND (volume >= 1.2 x
  SMA20(15m) OR lower wick >= 50% of the candle range).

BUY-SIDE sweep -> SHORT setup: mirror image above a zone's high.

Direction filter (2.6): longs when 4H bias is bullish, or range with the
zone at/below range mid; shorts mirrored. Counter-trend sweeps only when
`allow_countertrend` is true AND zone score >= 3 (default: disabled).

Pure detection function + a light stateful detector that de-duplicates
already-consumed sweep bars per (symbol, zone, sweep ts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from core.models import Candle


@dataclass
class SweepSignal:
    kind: str                # "sell_side" (long setup) | "buy_side" (short setup)
    direction: str           # "long" | "short" AFTER the direction filter
    zone_price: float
    zone_upper: float
    zone_lower: float
    zone_score: int
    wick_extreme: float      # sweep low (long) / sweep high (short)
    sweep_ts: object         # open time of the sweep candle
    confirm_ts: object       # open time of the reclaim-confirming candle
    volume_ok: bool
    wick_ok: bool
    features: dict = field(default_factory=dict)

    def features_dict(self) -> dict:
        f = {
            "kind": self.kind,
            "direction": self.direction,
            "zone_price": self.zone_price,
            "zone_upper": self.zone_upper,
            "zone_lower": self.zone_lower,
            "zone_score": self.zone_score,
            "wick_extreme": self.wick_extreme,
            "volume_ok": self.volume_ok,
            "wick_ok": self.wick_ok,
        }
        f.update(self.features)
        return f


def effective_bias(bias: str) -> str:
    """Range bias names collapse to their lean for filter logic."""
    return {
        "range_long": "range_long",
        "range_short": "range_short",
    }.get(bias, bias)


def filter_sweep_side(
    *,
    wanted_direction: str,          # "long" from sell-side sweep, "short" mirror
    bias: str,                      # "bullish"|"bearish"|"range_long"|"range_short"|"range"
    zone_price: float,
    range_mid: Optional[float],
    zone_score: int,
    allow_countertrend: bool = False,
    counter_min_score: int = 3,
) -> Optional[str]:
    """Return `wanted_direction` when the direction filter allows it, else None.

    Bias "range_long"/"range_short" (EMA200 tiebreak) behaves like range: the
    at/below-mid rule decides — a range_short range still allows longs when
    the zone sits at/below the range mid, etc.
    """
    if bias == "bullish":
        allowed_long = True
        allowed_short = False
    elif bias == "bearish":
        allowed_long = False
        allowed_short = True
    else:  # all range flavours
        if range_mid is None:
            # no range defined yet: fall back to the EMA200 tiebreak lean;
            # a plain "range" (no lean available) allows both sides
            allowed_long = bias in ("range_long", "range")
            allowed_short = bias in ("range_short", "range")
        else:
            allowed_long = zone_price <= range_mid
            allowed_short = zone_price >= range_mid

    ok = allowed_long if wanted_direction == "long" else allowed_short
    if ok:
        return wanted_direction
    # counter-trend: enabled config AND strong zone
    if allow_countertrend and zone_score >= counter_min_score:
        return wanted_direction
    return None


def _sma20_volume(candles_before: Sequence[Candle], length: int = 20) -> Optional[float]:
    """SMA of the closing volume of the bars BEFORE the sweep bar."""
    vols = [float(c.volume) for c in candles_before[-length:]]
    if len(vols) < length:
        return None  # insufficient history -> volume condition via wick checks
    return float(sum(vols) / length)


def detect_sweep(
    candles: Sequence[Candle],
    zone,                        # levels.Zone
    atr_1h_value: float,
    *,
    sweep_min_atr: float = 0.10,
    reclaim_bars: int = 3,
    volume_mult: float = 1.2,
    wick_frac_before: float = 0.5,
    filter_direction: bool = True,
    bias: str = "range",
    range_mid: Optional[float] = None,
    allow_countertrend: bool = False,
    volume_sma_len: int = 20,
) -> Optional[SweepSignal]:
    """Scan CLOSED 15m candles for the freshEST valid sweep setup.

    Returns the newest confirmed signal, or None. "Confirmed" means the
    reclaim candle exists within reclaim_bars after the sweep bar (and may be
    the latest candle itself).
    """
    n = len(candles)
    if n < 3 or atr_1h_value <= 0:
        return None
    w = min(n, reclaim_bars + 8)  # window must hold a sweep + its reclaim
    window = candles[-w:]
    base = n - len(window)  # global index of window[0]

    best: Optional[SweepSignal] = None
    for i, c in enumerate(window):
        vol_sma = _sma20_volume(candles[: base + i], volume_sma_len)
        depth = sweep_min_atr * atr_1h_value

        kind = None
        wick_extreme = 0.0
        volume_ok = wick_ok = False
        low_breach = zone.lower - float(c.low)
        high_breach = float(c.high) - zone.upper
        depth = sweep_min_atr * atr_1h_value
        if low_breach >= high_breach and float(c.low) < zone.lower - depth:
            # sell-side sweep -> long candidate
            kind, wick_extreme = "sell_side", float(c.low)
            lw = min(float(c.close), float(c.open)) - float(c.low)
            rng = max(float(c.high) - float(c.low), 1e-12)
            wick_ok = (lw / rng) >= wick_frac_before
            volume_ok = vol_sma is not None and float(c.volume) >= volume_mult * vol_sma
        elif high_breach > low_breach and float(c.high) > zone.upper + depth:
            kind, wick_extreme = "buy_side", float(c.high)
            uw = float(c.high) - max(float(c.close), float(c.open))
            rng = max(float(c.high) - float(c.low), 1e-12)
            wick_ok = (uw / rng) >= wick_frac_before
            volume_ok = vol_sma is not None and float(c.volume) >= volume_mult * vol_sma
        if kind is None or not (volume_ok or wick_ok):
            continue

        # reclaim within reclaim_bars after the sweep bar
        for j in range(i + 1, min(i + 1 + reclaim_bars, len(window))):
            rc = window[j]
            if kind == "sell_side" and float(rc.close) > zone.price:
                wanted = "long"
            elif kind == "buy_side" and float(rc.close) < zone.price:
                wanted = "short"
            else:
                continue
            direction = wanted
            if filter_direction:
                direction = filter_sweep_side(
                    wanted_direction=wanted,
                    bias=bias,
                    zone_price=zone.price,
                    range_mid=range_mid,
                    zone_score=zone.score,
                    allow_countertrend=allow_countertrend,
                )
                if direction is None:
                    break  # direction filter blocks this sweep
            sig = SweepSignal(
                kind=kind,
                direction=direction,
                zone_price=zone.price,
                zone_upper=zone.upper,
                zone_lower=zone.lower,
                zone_score=zone.score,
                wick_extreme=wick_extreme,
                sweep_ts=c.ts,
                confirm_ts=rc.ts,
                volume_ok=volume_ok,
                wick_ok=wick_ok,
                features={"atr_1h": atr_1h_value},
            )
            best = sig  # keep scanning: later sweep bars are "fresher"
            break
    if best is not None:
        best.features["entries"] = {
            "sweep_min_atr": sweep_min_atr,
            "reclaim_bars": reclaim_bars,
            "volume_mult": volume_mult,
            "wick_frac_before": wick_frac_before,
        }
    return best


class SweepDetector:
    """De-duplicates consumed sweep bars per (symbol, zone, sweep ts); the
    closed-candle series is supplied by the CALLER (the strategy's trigger-TF
    buffer including the just-closed candle), keeping ATR/SMA20 consistent
    with warmup data."""

    def __init__(self, max_keys: int = 200) -> None:
        self.max_keys = max_keys
        self._consumed: dict[str, set[str]] = {}

    def feed(
        self,
        symbol: str,
        candles: Sequence[Candle],      # closed candles incl. the new one
        zone,
        atr_1h_value: float,
        *,
        sweep_min_atr: float,
        reclaim_bars: int,
        volume_mult: float,
        wick_frac: float,
        bias: str,
        range_mid: Optional[float],
        allow_countertrend: bool,
        volume_sma_len: int = 20,
    ) -> Optional[SweepSignal]:
        sig = detect_sweep(
            candles,
            zone,
            atr_1h_value,
            sweep_min_atr=sweep_min_atr,
            reclaim_bars=reclaim_bars,
            volume_mult=volume_mult,
            wick_frac_before=wick_frac,
            bias=bias,
            range_mid=range_mid,
            allow_countertrend=allow_countertrend,
            volume_sma_len=volume_sma_len,
        )
        if sig is None:
            return None
        key = f"{zone.price}:{sig.sweep_ts}"
        consumed = self._consumed.setdefault(symbol, set())
        if key in consumed:
            return None
        consumed.add(key)
        if len(consumed) > self.max_keys:
            self._consumed[symbol] = set(
                sorted(consumed, key=str)[-self.max_keys // 2 :])
        return sig
