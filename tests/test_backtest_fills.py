import sys
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "src")

from core.models import Candle, OrderRecord, Side
from backtest.fills import FillSimulator


def mk_candle(o, h, l, c, symbol="XAUTUSD"):
    ts = datetime.now(timezone.utc)
    return Candle(symbol=symbol, resolution="1m", ts=ts, open=o, high=h, low=l, close=c)


def mk_order(side, price, size=10):
    return OrderRecord(
        client_order_id="x-1",
        symbol="XAUTUSD",
        side=side,
        size=size,
        limit_price=Decimal(str(price)),
        status="open",
    )


def test_buy_limit_fills_only_when_traded_through():
    sim = FillSimulator(tick_sizes={"XAUTUSD": Decimal("0.1")})
    o = mk_order(Side.BUY, 100.0)
    # touches but does not trade through -> no fill (touch fill disabled)
    assert sim.limit_fill(o, mk_candle(100.5, 100.6, 100.0, 100.2)).filled is False
    # trades through by 1 tick -> fill
    r = sim.limit_fill(o, mk_candle(100.5, 100.6, 99.9, 100.2))
    assert r.filled and r.fill_price == Decimal("100.0")


def test_sell_limit_mirror():
    sim = FillSimulator(tick_sizes={"XAUTUSD": Decimal("0.1")})
    o = mk_order(Side.SELL, 100.0)
    assert sim.limit_fill(o, mk_candle(99.5, 99.9, 99.4, 99.8)).filled is False
    assert sim.limit_fill(o, mk_candle(99.8, 100.1, 99.4, 99.9)).filled


def test_touch_fill_prob():
    sim = FillSimulator(tick_sizes={"XAUTUSD": Decimal("0.1")}, touch_fill_prob=1.0)
    o = mk_order(Side.BUY, 100.0)
    r = sim.limit_fill(o, mk_candle(100.5, 100.6, 100.0, 100.2))
    assert r.filled


def test_market_fill_slippage_and_tick_rounding():
    sim = FillSimulator(tick_sizes={"XAUTUSD": Decimal("0.1")}, slippage_bps=Decimal("10"))
    px = sim.market_fill_price(Side.BUY, Decimal("100.0"), Decimal("0.1"))
    assert px == Decimal("100.1")  # 100 + 0.1% => 100.1 rounded UP to tick
    px_s = sim.market_fill_price(Side.SELL, Decimal("100.0"), Decimal("0.1"))
    assert px_s == Decimal("99.9")


def test_stop_trigger():
    sim = FillSimulator()
    o = mk_order(Side.SELL, 98.0)  # sell stop below market
    assert sim.stop_market_trigger(o, mk_candle(99, 100, 97.9, 98.5))
    assert not sim.stop_market_trigger(o, mk_candle(99, 100, 98.5, 99.5))
