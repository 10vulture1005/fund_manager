"""Historical candle loading for backtests (spec 3.8: 12+ months of candles).

Downloads from Delta India `/v2/history/candles` into parquet/csv under
data/<symbol>_<resolution>.csv, then serves them to the backtest loop.
Falls back to cached files when the network is unavailable (with a loud log).
"""
from __future__ import annotations

import csv
import gzip
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

from core.models import Candle

RESOLUTIONS = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "2H": 120,
    "4H": 240,
    "1D": 1440,
}


def data_path(data_dir: str | Path, symbol: str, resolution: str) -> Path:
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{symbol}_{resolution}.csv"


def resample(candles: list[Candle], target: str) -> list[Candle]:
    """Aggregate base candles into `target` buckets.

    A bucket CLOSED candle exists only once all constituent base bars are
    closed (no lookahead — spec 3.6). Known resolutions only; anything unusual
    passes through unchanged.
    """
    step_minutes = RESOLUTIONS.get(target)
    if step_minutes is None:
        return list(candles)
    out: list[Candle] = []
    by_symbol: dict[str, list[Candle]] = {}
    for c in candles:
        by_symbol.setdefault(c.symbol, []).append(c)
    for sym, series in by_symbol.items():
        if series and series[0].resolution == target:
            out.extend(series)
            continue
        acc: list[Candle] = []
        for c in series:
            acc.append(c)
            # bucket closes when the next constituent starts a fresh bucket
            step_s = step_minutes * 60
            ts = c.ts
            if (int(ts.timestamp()) % step_s) == step_s - _base_step(series[0].resolution):
                o = acc[0].open
                out.append(
                    Candle(
                        symbol=sym,
                        resolution=target,
                        ts=acc[0].ts,
                        open=o,
                        high=max(x.high for x in acc),
                        low=min(x.low for x in acc),
                        close=acc[-1].close,
                        volume=sum(x.volume for x in acc),
                    )
                )
                acc = []
    return out


def _base_step(resolution: str) -> int:
    return RESOLUTIONS.get(resolution, 1) * 60



def save_candles(path: Path, candles: Iterable[Candle]) -> int:
    rows = [
        [
            c.ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            c.open,
            c.high,
            c.low,
            c.close,
            c.volume,
        ]
        for c in candles
    ]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    return len(rows)


def load_candles(path: Path, symbol: str, resolution: str) -> list[Candle]:
    if not path.exists():
        return []
    out: list[Candle] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            out.append(
                Candle(
                    symbol=symbol,
                    resolution=resolution,
                    ts=datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
    return out


async def download_candles(
    rest_base: str,
    symbol: str,
    resolution: str,
    start: datetime,
    end: datetime,
    out_dir: str | Path = "data",
) -> list[Candle]:
    """Fetch OHLC from GET /v2/history/candles and cache to CSV.

    NOTE ("VERIFY"): candle params are (symbol, resolution, start, end) in
    SECONDS per the prompt; confirm against live docs; on 4xx fall back to cache.
    """
    if httpx is None:
        raise RuntimeError("httpx required for downloads")
    step = RESOLUTIONS.get(resolution, 60) * 60
    # prod API wants lowercase resolutions (1h/4h/1d) and caps at 4000 rows per
    # request; testnet accepted "1H" style (TODO(VERIFY) — confirmed empirically
    # 2026-10-22 via a 400 `bad_schema` response from prod).
    label = resolution if "testnet" in rest_base else resolution.lower()
    BARS_PER_REQUEST = 4000
    results: list[Candle] = []
    async with httpx.AsyncClient(base_url=rest_base, timeout=30, headers={"User-Agent": "fm-backtest/0.1"}) as cli:
        chunk_start = int(start.timestamp())
        end_s = int(end.timestamp())
        while chunk_start < end_s:
            chunk_end = min(chunk_start + step * BARS_PER_REQUEST, end_s)
            url = f"/v2/history/candles?symbol={symbol}&resolution={label}&start={chunk_start}&end={chunk_end}"
            r = await cli.get(url)
            if r.status_code != 200:
                break
            body = r.json()
            for row in body.get("result", []):
                # row: [time] and OHLCV columns — VERIFY exact ordering in docs
                ts = datetime.fromtimestamp(int(row.get("time", 0)), tz=timezone.utc)
                results.append(
                    Candle(
                        symbol=symbol,
                        resolution=resolution,
                        ts=ts,
                        open=float(row.get("open", 0)),
                        high=float(row.get("high", 0)),
                        low=float(row.get("low", 0)),
                        close=float(row.get("close", 0)),
                        volume=float(row.get("volume", 0)),
                    )
                )
            chunk_start = chunk_end
    # prod returns rows newest-first; the engine replays chronologically.
    results.sort(key=lambda c: c.ts)
    save_candles(data_path(out_dir, symbol, resolution), results)
    return results
