"""Regime model (spec 2.9).

For each underlying (ETH, BTC, XAUT, each stock symbol):
- trend score from close vs EMA50/EMA200 (1D) plus the EMA50 10-day slope,
  normalised by ATR(14, 1D) -> {strong_down, down, neutral, up, strong_up}
- volatility = ATR(14,1D)/close and its 90-day percentile
- drawdown from the 90-day high.

Computed daily at 00:00 UTC and re-evaluated on every 4H close (same 1D
arithmetic, lagged — see ASSUMPTIONS).

`HistoryStore` holds closed-candle frames (shared by sleeves + regime).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from pydantic import BaseModel

from core.indicators import atr, ema, rolling_percentile
from core.models import Candle
from strategy.all_in_one.defaults import get


class HistoryStore:
    """Keeps per (symbol, resolution) frames of CLOSED candles only."""

    def __init__(self) -> None:
        self.frames: dict[tuple[str, str], pd.DataFrame] = {}

    def upsert(self, candle: Candle) -> None:
        if not candle.closed:
            return
        key = (candle.symbol, candle.resolution)
        df = self.frames.setdefault(
            key,
            pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
            .set_index("ts"),
        )
        row = pd.DataFrame(
            {
                "open": [float(candle.open)],
                "high": [float(candle.high)],
                "low": [float(candle.low)],
                "close": [float(candle.close)],
                "volume": [float(candle.volume)],
            },
            index=[candle.ts],
        )
        df.index = pd.to_datetime(df.index, utc=True)
        row.index = pd.to_datetime(row.index, utc=True)
        if candle.ts in df.index:
            df.loc[candle.ts] = [float(candle.open), float(candle.high),
                                 float(candle.low), float(candle.close),
                                 float(candle.volume)]
        else:
            self.frames[key] = pd.concat([df, row]).sort_index()
        
    def frame(self, symbol: str, resolution: str) -> pd.DataFrame:
        return self.frames.get((symbol, resolution), pd.DataFrame())


class RegimeSnapshot(BaseModel):
    symbol: str
    ts: datetime | None = None
    trend: str = "neutral"        # strong_down | down | neutral | up | strong_up
    score: float = 0.0
    vol: float = 0.0              # ATR(14,1D)/close
    vol_pctl: float = 50.0        # 0..100 percentile over the last `vol_lookback` obs
    drawdown: float = 0.0         # fraction from the 90d high (>= 0)
    atr: float = 0.0
    close: float = 0.0

    @property
    def high_vol(self) -> bool:
        return self.vol_pctl > 85.0


def _series_or_none(s: pd.Series) -> pd.Series:
    return s if isinstance(s, pd.Series) else pd.Series(dtype=float)


def compute_snapshot(df: pd.DataFrame, symbol: str, cfg: dict) -> RegimeSnapshot | None:
    """Regime snapshot from a frame of closed 1D candles."""
    if df.empty or len(df) < 10:
        return None
    close = df["close"].astype(float)
    ema_fast_n = int(get(cfg, "regime.ema_fast", 50))
    ema_slow_n = int(get(cfg, "regime.ema_slow", 200))
    slope_n = int(get(cfg, "regime.slope_days", 10))
    atrp = int(get(cfg, "regime.vol_atr_period", 14))
    vol_lb = int(get(cfg, "regime.vol_lookback", 90))
    dd_lb = int(get(cfg, "regime.dd_lookback", 90))

    ef = ema(close, ema_fast_n)
    es = ema(close, ema_slow_n)
    a14 = atr(df, atrp)

    last = close.index[-1]
    c = float(close.iloc[-1])
    # current values must be available; EMA slow needs >= ~2*span to be meaningful
    if pd.isna(ef.iloc[-1]) or pd.isna(es.iloc[-1]) or pd.isna(a14.iloc[-1]) or a14.iloc[-1] <= 0:
        return None
    a = float(a14.iloc[-1])
    d_fast = (c - float(ef.iloc[-1])) / a
    d_slow = (c - float(es.iloc[-1])) / a
    j = -slope_n - 1
    e_prev = float(ef.iloc[j]) if abs(j) < len(ef) and pd.notna(ef.iloc[j]) else None
    if e_prev and e_prev > 0:
        d_slope = (float(ef.iloc[-1]) - e_prev) / a
    else:
        d_slope = 0.0

    comp_cap = float(get(cfg, "regime.component_cap", 2.0))

    def comp(x: float) -> float:
        sign = 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)
        return sign * min(abs(x), comp_cap)

    score = 0.5 * comp(d_fast) + 0.25 * comp(d_slow) + 0.25 * comp(d_slope)
    neutral = float(get(cfg, "regime.neutral_band", 0.5))
    strong = float(get(cfg, "regime.strong_band", 2.0))
    if score >= strong:
        trend = "strong_up"
    elif score > neutral:
        trend = "up"
    elif score <= -strong:
        trend = "strong_down"
    elif score < -neutral:
        trend = "down"
    else:
        trend = "neutral"

    vol_series = a14 / close
    vp = rolling_percentile(vol_series, vol_lb)
    vp_last = float(vp.iloc[-1]) * 100.0 if pd.notna(vp.iloc[-1]) else 50.0

    peak = close.rolling(dd_lb, min_periods=1).max()
    drawdown = (float(peak.iloc[-1]) - c) / float(peak.iloc[-1])

    return RegimeSnapshot(
        symbol=symbol, ts=last, trend=trend, score=float(score), vol=float(vol_series.iloc[-1]),
        vol_pctl=vp_last, drawdown=drawdown, atr=a, close=c,
    )


def trend_to_status(mapping: dict[str, str], trend: str, default: str = "PAUSED") -> str:
    return mapping.get(trend, default)


class RegimeEngine:
    """Owns the per-underlying regime snapshots."""

    def __init__(self, cfg: dict, store: HistoryStore) -> None:
        self.cfg = cfg
        self.store = store
        self.snapshots: dict[str, RegimeSnapshot] = {}

    def refresh(self, symbol: str) -> RegimeSnapshot | None:
        snap = compute_snapshot(self.store.frame(symbol, "1D"), symbol, self.cfg)
        if snap is not None:
            self.snapshots[symbol] = snap
        return snap

    def get(self, symbol: str) -> RegimeSnapshot | None:
        return self.snapshots.get(symbol)

    def sleeve_status(self, sleeve: str, symbol: str) -> str:
        """Sleeve status from the regime maps (spec 2.9)."""
        snap = self.snapshots.get(symbol)
        if snap is None:
            return "PAUSED"
        key = {"ETH_CC": "eth_cc_map", "BTC_CP": "btc_cp_map"}.get(sleeve)
        if key:
            return trend_to_status(dict(get(self.cfg, f"regime.{key}", {}) or {}), snap.trend)
        return "ACTIVE"

    def gold_mode(self, symbol: str) -> str:
        """GOLD sleeve mode by regime (spec 2.8): CC in neutral/up, CP in down."""
        snap = self.snapshots.get(symbol)
        if snap is None:
            return "NEUTRAL_WAIT"
        if snap.trend in ("up", "strong_up", "neutral"):
            return "CC_MODE"
        return "CP_MODE"
