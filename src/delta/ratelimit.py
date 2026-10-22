"""Client-side rate limiter for Delta Exchange India (spec 1.4).

- Quota: 20,000 weight per fixed 5-minute window per user.
- Weights: 3 for products / orderbook / tickers / open orders / margins /
  balances / candles, 5 for place / edit / delete order and add-position
  margin, 10 for order history / fills / transaction logs, 25 for batch APIs,
  everything else 1.
- We self-limit to ~60% of the quota so real 429s never hit us first.
- HTTP 429 responses come with `X-RATE-LIMIT-RESET` (milliseconds); we honour
  it by blocking until reset.
- Per-product matching engine cap: 500 operations / second / product; we
  target ~60% of it as well. Cancels are exempt from that cap (spec 1.4).

Design notes: the limiter is async and pure with injectable clock + sleep
callables, so tests can drive it with a fake clock and no real waiting.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable


import re


# Non-cancellable operation = every op consumed against the per-product cap.
_MAX_OPS_PER_PRODUCT_S = 500.0

_EPS = 1e-6


def weight_for(method: str, path: str) -> int:
    """Weight of one REST call (spec 1.4). `path` may be the raw endpoint
    (leading slash, /v2 optional). Unknown endpoints cost the default 1.
    """
    p = path.split("?", 1)[0]
    # normalise leading /v2 (docs paths are usually written without it)
    if p.startswith("/v2"):
        p = p[3:]
    p = p.rstrip("/") or "/"

    if method == "DELETE":
        if p == "/orders/batch" or p == "/orders/all":
            return 25
        return 5  # delete order
    if method in ("POST", "PUT"):
        if p == "/orders/batch":
            return 25
        if p in ("/orders", "/orders/edit", "/orders/bracket") or p.startswith(
            ("/orders/", "/positions/")
        ):
            return 5
        return 1
    # GET
    if p in (
        "/products",
        "/l2orderbook",
        "/tickers",
        "/orders",  # open orders listing
        "/positions",
        "/positions/margined",
        "/wallet/balances",
        "/history/candles",
    ) or p.startswith(("/products/", "/tickers/", "/l2orderbook/")):
        return 3
    if p in ("/orders/history", "/fills", "/wallet/transactions"):
        return 10
    return 1


class RateLimiter:
    """Sliding-window weight budget + per-product op cap, both ~60% of quota."""

    def __init__(
        self,
        max_weight_per_window: int = 20_000,
        window_seconds: float = 300.0,
        quota_fraction: float = 0.60,
        product_ops_cap: float = _MAX_OPS_PER_PRODUCT_S * 0.60,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        if quota_fraction <= 0 or quota_fraction >= 1:
            raise ValueError("quota_fraction must be in (0, 1); e.g. 0.6 for ~60% of quota")
        self.window = float(window_seconds)
        self.limit = int(max_weight_per_window * quota_fraction)
        self.product_ops_cap = float(product_ops_cap)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        # sliding window of (ts, weight)
        self._events: deque[tuple[float, int]] = deque()
        # per-product sliding 1s op window: product_id -> deque[ts]
        self._product_ops: dict[int | str, deque[float]] = {}
        self._blocked_until: float | None = None
        self.total_weight_granted = 0

    # ------------------------------------------------------------- introspection
    def used_in_window(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        self._prune(now)
        return sum(w for _, w in self._events)

    def _prune(self, now: float) -> None:
        horizon = now - self.window
        while self._events and self._events[0][0] <= horizon:
            self._events.popleft()

    # ------------------------------------------------------------------ 429 path
    def register_reset_header(self, milliseconds: float | str) -> None:
        """Honour `X-RATE-LIMIT-RESET` (ms until calls may resume, spec 1.4)."""
        ms = float(milliseconds)
        reset_at = self._clock() + ms / 1000.0
        if self._blocked_until is None or reset_at > self._blocked_until:
            self._blocked_until = reset_at

    # ------------------------------------------------------------ per-product cap
    def _product_blocked_for(self, product_id: int | str, ops: float, now: float) -> float | None:
        q = self._product_ops.setdefault(product_id, deque())
        while q and q[0] <= now - 1.0:
            q.popleft()
        if len(q) + ops > self.product_ops_cap:
            # wait until the oldest op slides out of the 1s window
            return max(q[0] + 1.0 - now, _EPS)
        return None

    def _grant_product_ops(self, product_id: int | str, ops: float, now: float) -> None:
        q = self._product_ops.setdefault(product_id, deque())
        q.extend([now] * int(ops))

    # --------------------------------------------------------------- acquisition
    async def acquire(
        self,
        weight: int | None = None,
        method: str | None = None,
        path: str | None = None,
        product_id: int | str | None = None,
        ops: float | None = None,
        exempt_from_product_cap: bool = False,
    ) -> None:
        """Await until one call of `weight` fits in every budget.

        `weight` overrides the path-derived weight (spec 1.4). `ops` counts
        operations against the per-product matching-engine cap (a batch of 50
        orders counts 50 ops); cancellations pass `exempt_from_product_cap=True`.
        """
        w = int(weight) if weight is not None else (weight_for(method or "", path or ""))
        if w > self.limit:
            raise ValueError(f"weight {w} can never fit in the self-limit {self.limit}")
        ops_val = 1.0 if ops is None else float(ops)
        while True:
            now = self._clock()
            if self._blocked_until is not None and self._blocked_until > now:
                await self._sleep(self._blocked_until - now + _EPS)
                self._blocked_until = None
                continue
            self._prune(now)
            used = self.used_in_window(now)
            if used + w <= self.limit:
                break
            # sleep until the oldest event leaves the window, then re-check
            oldest = self._events[0][0]
            wait = oldest + self.window - now + _EPS
            if wait <= _EPS:  # float slack guard; re-loop
                await self._sleep(_EPS)
                continue
            await self._sleep(wait)
        now = self._clock()
        self._events.append((now, w))
        self.total_weight_granted += w
        if product_id is not None and not exempt_from_product_cap:
            while True:
                now = self._clock()
                blocked = self._product_blocked_for(product_id, ops_val, now)
                if blocked is None:
                    self._grant_product_ops(product_id, ops_val, now)
                    break
                await self._sleep(blocked)
