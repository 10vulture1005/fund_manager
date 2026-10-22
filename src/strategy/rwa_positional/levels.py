"""Key levels and confluence-scored zones (spec 2.5).

Pure functions on closed-candle lists / floats, unit-testable without a market.

Candidates:
  - previous-day high/low, previous-week high/low
  - last N (default 10) confirmed swing highs/lows on 4H
  - equal-high / equal-low clusters: >= 2 swing points within
    `equal_cluster_atr_mult (0.15) x ATR(1H)` of each other
  - round numbers (config, OFF by default)

Score: +1 per independent confluence category:
  {htf_swing, prev day|week, equal cluster, >= 2 prior touches, round number}.
A high-conviction zone = level ± 0.25 x ATR(1H) with score >= min_zone_score.

Pandas indicators come from core.indicators (infra-owned, no lookahead).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import pandas as pd

import core.indicators as ind

from core.models import Candle


# --------------------------------------------------------------------- utils
def candles_df(candles: Sequence[Candle], with_volume: bool = False) -> pd.DataFrame:
    rows = {"high": [float(c.high) for c in candles],
            "low": [float(c.low) for c in candles],
            "close": [float(c.close) for c in candles]}
    if with_volume:
        rows["volume"] = [float(c.volume) for c in candles]
    return pd.DataFrame(rows)


def atr_from_candles(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """Last Wilder ATR of a CLOSED candle list (None until `period + 1` bars)."""
    if len(candles) < period + 1:
        return None
    s = ind.atr(candles_df(candles), period)
    v = s.iloc[-1]
    return None if pd.isna(v) else float(v)


def pivots_from_candles(
    candles: Sequence[Candle], left: int, right: int
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed swing pivots -> ([(idx,high)], [(idx,low)]).

    Only the last `swing_lookback` returned lists are used by the strategy;
    here we return all of them. Index = positional index into `candles`.
    """
    n = len(candles)
    if n < left + right + 1:
        return [], []
    df = candles_df(candles)
    pv = ind.pivots(df["high"], df["low"], left=left, right=right)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for bar, kind, price, _confirmed in pv.reset_index().itertuples(index=False):
        pos = int(bar)
        if kind == "high":
            highs.append((pos, float(price)))
        else:
            lows.append((pos, float(price)))
    return highs, lows


def swing_points_all(
    candles: Sequence[Candle], left: int, right: int
) -> list[tuple[int, float]]:
    """All swing points (highs AND lows, untagged) as (idx, price)."""
    hi, lo = pivots_from_candles(candles, left, right)
    pts = [(i, p) for i, p in hi] + [(i, p) for i, p in lo]
    return sorted(pts)


# ---------------------------------------------------------------- data types
@dataclass
class Level:
    price: float
    score: int = 0
    sources: set[str] = field(default_factory=set)


@dataclass
class Zone:
    price: float          # the raw level
    score: int
    upper: float          # level + zone_atr_mult x ATR(1H)
    lower: float          # level - zone_atr_mult x ATR(1H)
    sources: set[str] = field(default_factory=set)

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def features(self) -> dict[str, Any]:
        return {
            "price": self.price, "score": self.score,
            "upper": self.upper, "lower": self.lower,
            "sources": sorted(self.sources),
        }


# ---------------------------------------------------------------- pure maths
def equal_high_low_clusters(
    swing_pts: Sequence[tuple[int, float]], tol: float
) -> list[list[tuple[int, float]]]:
    """Group swing points within `tol` of their cluster (greedy time scan)."""
    clusters: list[list[tuple[int, float]]] = []
    for idx, price in sorted(swing_pts, key=lambda x: x[0]):
        for cl in clusters:
            if abs(cl[-1][1] - price) <= tol:
                cl.append((idx, price))
                break
        else:
            clusters.append([(idx, price)])
    return [c for c in clusters if len(c) >= 2]


def round_number_levels(ref_price: float, step_pct: float, girdle_pct: float = 20.0) -> list[float]:
    """Round-number grid around ref_price (optional feature, off by default)."""
    if ref_price <= 0 or step_pct <= 0:
        return []
    step = ref_price * step_pct / 100.0
    lo, hi = ref_price * (1 - girdle_pct / 100), ref_price * (1 + girdle_pct / 100)
    k_lo, k_hi = int(max(lo, 0) / step), int(hi / step)
    return [step * k for k in range(k_lo, k_hi + 1)]


def touch_count(candles: Sequence[Candle], level: float, tol: float) -> int:
    """Distinct price-touch EPISODES of `level` (>= 2 counts one score point).

    An episode = consecutive candles whose range intersects
    [level - tol, level + tol]; consecutive touching candles are ONE touch.
    """
    episodes = 0
    in_touch = False
    for c in candles:
        near = (c.low - tol) <= level <= (c.high + tol)
        if near and not in_touch:
            episodes += 1
        in_touch = near
    return episodes


# ------------------------------------------------------------------- builder
def build_levels(
    *,
    candles_1h: Sequence[Candle],
    atr_1h_value: float,
    swings_4h: Sequence[tuple[int, float]],
    prev_day_high: Optional[float] = None,
    prev_day_low: Optional[float] = None,
    prev_week_high: Optional[float] = None,
    prev_week_low: Optional[float] = None,
    swing_lookback: int = 10,
    equal_cluster_atr_mult: float = 0.15,
    zone_atr_mult: float = 0.25,
    round_numbers: Optional[dict] = None,
) -> list[Level]:
    """Build + score candidate levels. Raw pieces are passed in already
    resolved (prev-day/week H/L floats; swing (idx, price) on 4H)."""
    assert atr_1h_value and atr_1h_value > 0.0
    cluster_tol = equal_cluster_atr_mult * atr_1h_value
    merge_tol = zone_atr_mult * atr_1h_value
    push_tol = merge_tol / 2.0  # two sources of ONE zone live within this

    raw: list[Level] = []

    def add(price: float, source: str) -> None:
        if price is None or price != price or price <= 0:
            return
        for lv in raw:
            if abs(lv.price - float(price)) <= push_tol:
                lv.sources.add(source)
                return
        raw.append(Level(price=float(price), score=0, sources={source}))

    if prev_day_high is not None:
        add(prev_day_high, "prev_day_high")
    if prev_day_low is not None:
        add(prev_day_low, "prev_day_low")
    if prev_week_high is not None:
        add(prev_week_high, "prev_week_high")
    if prev_week_low is not None:
        add(prev_week_low, "prev_week_low")

    swing_pts = [(i, float(p)) for i, p in list(swings_4h)[-swing_lookback:]]
    for _, price in swing_pts:
        add(price, "htf_swing")
    # equal-high/low clusters are their own confluence, centred on the mean
    for cl in equal_high_low_clusters(swing_pts, cluster_tol):
        add(sum(p for _, p in cl) / len(cl), "equal_cluster")

    if round_numbers and round_numbers.get("enabled"):
        midline = prev_day_high if prev_day_high is not None else (
            candles_1h[-1].close if candles_1h else 0.0)
        for rn in round_number_levels(
            float(midline), float(round_numbers.get("step_pct", 1.0))
        ):
            add(rn, "round_number")

    for lv in raw:
        score = 0
        if "htf_swing" in lv.sources:
            score += 1
        if lv.sources & {"prev_day_high", "prev_day_low", "prev_week_high", "prev_week_low"}:
            score += 1
        if "equal_cluster" in lv.sources:
            score += 1
        if "round_number" in lv.sources and (round_numbers or {}).get("enabled"):
            score += 1
        if touch_count(candles_1h, lv.price, merge_tol) >= 2:
            score += 1
        lv.score = score
    return raw


def select_zones(
    levels: Sequence[Level],
    atr_1h_value: float,
    min_zone_score: int = 2,
    zone_atr_mult: float = 0.25,
) -> list[Zone]:
    zones = [
        Zone(price=round(lv.price, 10), score=lv.score,
             upper=lv.price + zone_atr_mult * atr_1h_value,
             lower=lv.price - zone_atr_mult * atr_1h_value,
             sources=set(lv.sources))
        for lv in levels if lv.score >= min_zone_score
    ]
    zones.sort(key=lambda z: (-z.score, abs(z.price)))
    return zones


class Levels:
    """Facade matching the package export: build + score + select in one call."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        c = cfg or {}
        self.swing_lookback = int(c.get("level_swing_lookback", 10))
        self.equal_tol = float(c.get("equal_cluster_atr_mult", 0.15))
        self.zone_mult = float(c.get("zone_atr_mult", 0.25))
        self.min_score = int(c.get("min_zone_score", 2))
        self.round_numbers = c.get("round_numbers") or {"enabled": False}

    def build(
        self,
        *,
        candles_1h: Sequence[Candle],
        atr_1h_value: float,
        swings_4h: Sequence[tuple[int, float]],
        prev_day_high: Optional[float] = None,
        prev_day_low: Optional[float] = None,
        prev_week_high: Optional[float] = None,
        prev_week_low: Optional[float] = None,
    ) -> list[Zone]:
        lv = build_levels(
            candles_1h=candles_1h,
            atr_1h_value=atr_1h_value,
            swings_4h=swings_4h,
            prev_day_high=prev_day_high,
            prev_day_low=prev_day_low,
            prev_week_high=prev_week_high,
            prev_week_low=prev_week_low,
            swing_lookback=self.swing_lookback,
            equal_cluster_atr_mult=self.equal_tol,
            zone_atr_mult=self.zone_mult,
            round_numbers=self.round_numbers,
        )
        return select_zones(lv, atr_1h_value, self.min_score, self.zone_mult)
