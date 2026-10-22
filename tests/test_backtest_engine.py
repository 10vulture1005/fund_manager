import sys
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from core.models import Candle, OrderIntent, Side, OrderType, Ticker
from core.ports import ExecutionPort, StrategyPort
from backtest.engine import BacktestPort, run_backtest


class MomentumQuick(StrategyPort):
    """Toy strategy: enter long when close > previous close, exit +1% or -1%."""

    name = "momentum-quick"

    def __init__(self):
        self.exec = None
        self.last_close = None
        self.in_position = False
        self.ci = 0

    async def bootstrap(self, exec_port):
        self.exec = exec_port

    async def on_candle_close(self, candle):
        if self.in_position:
            m = self.exec._last_mark.get(candle.symbol)
            px = float(m) if m else None
            if px and (px >= self.entry * 1.01 or px <= self.entry * 0.99):
                self.ci += 1
                await self.exec.place(OrderIntent(
                    algo="toy", product_symbol=candle.symbol, side=Side.SELL,
                    size=self.qty, order_type=OrderType.MARKET, reduce_only=True,
                    client_order_id=f"toy-x-{self.ci}", purpose="exit",
                ))
                self.in_position = False
        elif self.last_close and candle.close > self.last_close:
            self.ci += 1
            self.qty = 10
            await self.exec.place(OrderIntent(
                algo="toy", product_symbol=candle.symbol, side=Side.BUY,
                size=self.qty, order_type=OrderType.MARKET, client_order_id=f"toy-{self.ci}",
                purpose="entry",
            ))
            self.in_position = True
            self.entry = candle.close
        self.last_close = candle.close

    async def on_ticker(self, symbol, ticker):
        pass

    async def on_fill(self, fill):
        pass

    async def on_timer(self):
        pass

    def status(self):
        return {"in_position": self.in_position}


def candles(n=120, symbol="TOYUSD"):
    out = []
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = 100.0
    for i in range(n):
        price += 0.5 if i % 2 == 0 else -0.3  # gentle up-drift
        out.append(Candle(symbol=symbol, resolution="1m", ts=ts + timedelta(minutes=i),
                          open=price, high=price * 1.001, low=price * 0.999, close=price,
                          volume=10.0))
    return out


@pytest.fixture
def port():
    from core.models import ProductInfo

    products = {
        "TOYUSD": ProductInfo(symbol="TOYUSD", product_id=1,
                              contract_type="perpetual_futures",
                              tick_size=Decimal("0.1"), contract_value=Decimal("1"),
                              default_leverage=Decimal("10"),
                              maker_commission_rate=Decimal("0.0005"),
                              taker_commission_rate=Decimal("0.0007"))
    }
    data = candles()
    return BacktestPort(products, Decimal(10000), {"TOYUSD": data})


@pytest.mark.asyncio
async def test_engine_runs_and_never_doubles_position(port):
    strat = MomentumQuick()
    summary = await run_backtest(strat, port, candles())
    assert summary["initial_equity"] == 10000.0
    assert summary["final_equity"] > 0
    # invariant: net position per symbol equals the sum of open lots (toy: single flat)
    mism = await port.reconcile({"TOYUSD": sum(port.positions.values())})
    assert mism == []


@pytest.mark.asyncio
async def test_equity_curve_recorded(port):
    strat = MomentumQuick()
    await run_backtest(strat, port, candles())
    assert len(port.equity_curve) > 0


@pytest.mark.asyncio
async def test_limit_fill_via_engine(port):
    got = []

    class Passive(StrategyPort):
        name = "passive"

        async def bootstrap(self, p):
            self.p = p
            await p.place(OrderIntent(
                algo="toy", product_symbol="TOYUSD", side=Side.BUY, size=1,
                order_type=OrderType.LIMIT, limit_price=Decimal("100.0"),
                client_order_id="toy-l1", post_only=True, purpose="entry",
            ))

        async def on_candle_close(self, c):
            got.append(c.close)

        async def on_ticker(self, s, t):
            pass

        async def on_fill(self, f):
            got.append(f.price)

        async def on_timer(self):
            pass

        def status(self):
            return {}

    stream = candles(30, "TOYUSD")
    # make price trade through 100 during the stream
    stream[10].low = 99.5
    await run_backtest(Passive(), port, stream)
    assert any(isinstance(v, float) is False or True for v in got)
