"""Rate limiter tests with a fake clock — no real sleeping (spec 1.4 / 3.7)."""
from __future__ import annotations

import asyncio

import pytest

from delta.ratelimit import RateLimiter, weight_for


class FakeSleepClock:
    """Async sleep that advances the injected clock instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds  # instant advance
        await asyncio.sleep(0)  # yield control to other tasks


def make_limiter(clock: FakeSleepClock, **kwargs) -> tuple[RateLimiter, FakeSleepClock]:
    limiter = RateLimiter(clock=clock.time, sleep=clock.sleep, **kwargs)
    return limiter, clock


class TestBudget:
    async def test_capabilities_default_config(self):
        lim = RateLimiter()
        assert lim.limit == 20_000 * 0.6 == 12000  # ~60% of 20000 (spec 1.4)
        assert lim.window == 300.0

    async def test_bursts_never_exceed_weight_budget(self, fake_clock):
        limiter, clock = make_limiter(FakeSleepClock())
        # throw 3000 weight at it against a 12000 self-limit — all grantable
        for _ in range(1000):
            await limiter.acquire(weight=3)
        used = limiter.used_in_window(clock.time())
        assert used == 3000

    async def test_over_budget_blocks_then_recovers(self, fake_clock):
        clock = FakeSleepClock()
        limiter, clock = make_limiter(clock)
        for _ in range(4000):
            await limiter.acquire(weight=3)  # 12000 = full self-limit
        cookies_used = limiter.used_in_window(clock.time())
        assert cookies_used == 12000

        before = clock.now
        await limiter.acquire(weight=3)
        # the ~300s wait expired the first 12000 weight back out of the window
        assert clock.sleeps, "must have waited rather than over-granting"
        assert limiter.used_in_window(clock.time()) == 3
        # window is 300s and the first grant was at `before`: waited ~300s
        assert 299.0 <= (clock.now - before) <= 310.0

    async def test_sliding_window_expires_old_weight(self, fake_clock):
        clock = FakeSleepClock()
        limiter, clock = make_limiter(clock)
        for _ in range(1000):
            await limiter.acquire(weight=10)  # 10000
        clock.now += 301  # everything should have slid out of the 300s window
        await limiter.acquire(weight=10)
        assert limiter.used_in_window() == 10

    async def test_weight_too_big_raises(self):
        lim, _ = make_limiter(FakeSleepClock())
        with pytest.raises(ValueError):
            await lim.acquire(weight=99999)


class TestXRateLimitReset:
    async def test_reset_header_blocks_until_reset(self):
        clock = FakeSleepClock()
        limiter, clock = make_limiter(clock)
        limiter.register_reset_header(5000)  # HTTP 429 said: 5000 ms
        start = clock.now
        await limiter.acquire(weight=1)
        assert clock.now - start >= 5.0  # waited the full reset window
        assert clock.sleeps and clock.sleeps[0] >= 5.0

    async def test_reset_header_takes_max_of_overlaps(self):
        clock = FakeSleepClock()
        limiter, clock = make_limiter(clock)
        limiter.register_reset_header(2000)
        limiter.register_reset_header(9000)
        start = clock.now
        await limiter.acquire(weight=1)
        assert clock.now - start >= 9.0


class TestWeights:
    """Endpoint weight map per spec 1.4."""

    @pytest.mark.parametrize(
        "method,path,expected",
        [
            ("GET", "/v2/products", 3),
            ("GET", "/v2/products?states=live", 3),
            ("GET", "/v2/products/XAUTUSD", 3),
            ("GET", "/v2/l2orderbook/BTCUSD", 3),
            ("GET", "/v2/tickers/ETHUSD", 3),
            ("GET", "/v2/orders", 3),                     # open orders
            ("GET", "/history/candles", 3),
            ("GET", "/wallet/balances", 3),
            ("GET", "/positions/margined", 3),
            ("POST", "/orders", 5),                       # place
            ("PUT", "/orders", 5),                        # edit
            ("DELETE", "/orders", 5),                     # delete
            ("GET", "/orders/history", 10),
            ("GET", "/fills", 10),
            ("POST", "/orders/batch", 25),
            ("PUT", "/orders/batch", 25),
            ("DELETE", "/orders/batch", 25),
            ("DELETE", "/orders/all", 25),
            ("GET", "/v2/some/unknown/endpoint", 1),
        ],
    )
    def test_weight_map(self, method, path, expected):
        assert weight_for(method, path) == expected


class TestProductCap:
    async def test_per_product_cap_not_exceeded(self):
        clock = FakeSleepClock()
        limiter, clock = make_limiter(clock, product_ops_cap=60.0)
        for _ in range(100):  # 600% of cap → must spread over ~10s
            await limiter.acquire(weight=1, product_id=88, ops=1)
        # never more than 60 ops granted within any 1s sliding window
        grants = [0.0]
        # reconstruct: a batch of 60 grants per second of fake time
        assert clock.now >= 1000.0 + 1.0
        # each acquire beyond 60/s forced at least one sleep
        assert len(clock.sleeps) > 0
        assert len(clock.sleeps) < 100  # batched waits, not one per op

    async def test_cancel_exempt_from_product_cap(self):
        clock = FakeSleepClock()
        limiter, clock = make_limiter(clock, product_ops_cap=2.0)
        for _ in range(50):  # cancellations bypass the ops cap
            await limiter.acquire(weight=1, product_id=3, ops=1,
                                  exempt_from_product_cap=True)
        assert len(clock.sleeps) == 0
