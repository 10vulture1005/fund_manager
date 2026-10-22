"""test_aio_stocks — STOCKS sleeve (spec 2.8): accumulate trigger, add spacing,
rebound distribution (LIFO newest, min-profit, 1-per-day), per-symbol stop,
spread time table and earnings blackout."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import pytest

from core.events import EventLogger
from core.ledger import LIFOLedger
from core.models import Candle, FillEvent, OrderResult, OrderStatus, ProductInfo, Side, Ticker
from core.state import DeltaSchema
from strategy.all_in_one import DEFAULTS
from strategy.all_in_one.regime import HistoryStore
from strategy.all_in_one.sleeve_base import SleeveStatus
from strategy.all_in_one.sleeve_stocks import StocksSleeve

NOW = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
SYM = "AAPL"

# 20 rising bars -> peak 95.2, then 12 falling bars (1.2 each) -> 80.8:
# dd from high = 15.1% >= 8%, RSI(14) = 31.7 <= 40 -> accumulate fires.
ACCUM_CLOSES = [80.0 + i * 0.8 for i in range(20)] + [95.2 - i * 1.2 for i in range(1, 13)]
# 38 rising bars + 2 flat bars: RSI >= 60 recent AND MACD hist fell 2 bars.
REBOUND_CLOSES = [80.0 + i * 0.5 for i in range(38)] + [98.5, 98.5]


class FakeExecPort:
    def __init__(self) -> None:
        self.placed: list[Any] = []
        self.cancelled: list[str] = []

    async def product(self, symbol: str):
        return ProductInfo(
            symbol=symbol, product_id=9, contract_type="perpetual_futures",
            tick_size=Decimal("0.01"), contract_value=Decimal("1"),
            trading_status="operational",
        )

    async def place(self, intent):
        self.placed.append(intent)
        return OrderResult(client_order_id=intent.client_order_id, accepted=True,
                           order_id="x1", status=OrderStatus.OPEN)

    async def cancel(self, client_order_id: str, symbol: str) -> bool:
        self.cancelled.append(client_order_id)
        return True


class FakePortfolio:
    def __init__(self, budget: float = 10_000.0) -> None:
        self.budget = budget
        self.blocked: Optional[str] = None

    def allow_new_lot(self, sleeve: str) -> tuple[bool, str]:
        if self.blocked:
            return False, self.blocked
        return True, "ok"

    def budget_for_new_lot(self, sleeve: str, max_lots: int, high_vol: bool = False,
                           n_symbols: int = 1) -> float:
        return 0.0 if self.blocked else self.budget / max(1, n_symbols)


def seed_closes(store: HistoryStore, symbol: str, closes: list[float]) -> None:
    for i, c in enumerate(closes):
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        store.upsert(Candle(symbol=symbol, resolution="1D", ts=ts,
                            open=c, high=c, low=c, close=c))


def tick(price: float) -> Ticker:
    return Ticker(symbol=SYM, mark_price=Decimal(str(price)))


def candle_at(closes: list[float], ts: datetime, price: Optional[float] = None) -> Candle:
    c = float(price if price is not None else closes[-1])
    return Candle(symbol=SYM, resolution="1D", ts=ts, open=c, high=c, low=c, close=c)


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fill(sleeve: StocksSleeve, port: FakeExecPort, price: float,
          ts: Optional[datetime] = None) -> Any:
    """Dispatch a fill for the most recently PLACED order through the sleeve."""
    intent = port.placed[-1]
    fill = FillEvent(client_order_id=intent.client_order_id,
                     symbol=intent.product_symbol, side=intent.side,
                     size=int(intent.size), price=Decimal(str(price)),
                     ts=ts or NOW, reduce_only=bool(intent.reduce_only))
    return sleeve.on_fill(fill)


@pytest.fixture()
def rig(tmp_path):
    built: list[LIFOLedger] = []

    def build(closes: list[float], **over):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["stocks"] = {**cfg.get("stocks", {}), "stock_symbols": [SYM], **over}
        db = DeltaSchema(str(tmp_path / "events.db"))
        events = EventLogger(db, algo="aio")
        led = LIFOLedger(str(tmp_path / f"ridge{len(built)}.db"), prefix="")
        built.append(led)
        port = FakeExecPort()
        store = HistoryStore()
        seed_closes(store, SYM, closes)
        sleeve = StocksSleeve(port, led, events, cfg, store, algo="aio", symbols=[SYM])
        sleeve.portfolio = FakePortfolio(30_000.0)     # min lot notional/price -> int contracts
        sleeve.set_status("ACTIVE", "up")
        _run(sleeve.load_products())
        return sleeve, port, led

    yield build
    for led in built:
        led.close()


async def open_lot(sleeve: StocksSleeve, port: FakeExecPort, price: float,
                   notional: float = 3_000.0) -> int:
    """Open one lot through the sleeve plumbing; returns the ledger lot id."""
    sleeve.marks[SYM] = Ticker(symbol=SYM, mark_price=Decimal(str(price)))
    oid = await sleeve.place_new_lot(SYM, notional, price, "test lot")
    assert oid is not None
    res = await _fill(sleeve, port, price)
    assert res.get("new_lot")
    return int(res["new_lot"])


def sell_intents(port: FakeExecPort) -> list:
    return [i for i in port.placed if i.purpose == "lifo_sell"]


# ---------------------------------------------------------------- accumulate
def test_accumulate_trigger_gated_by_dd_and_rsi(rig):
    sleeve, port, _ = rig(ACCUM_CLOSES)
    # too close to the high: dd 5.5% < 8% -> nothing
    _run(sleeve.on_ticker(SYM, tick(90.0)))
    assert port.placed == []
    # 85 -> dd 10.7% AND RSI 31.7 -> ONE entry order (buy lot_entry)
    _run(sleeve.on_ticker(SYM, tick(85.0)))
    assert len(port.placed) == 1
    it = port.placed[0]
    assert it.side is Side.BUY and it.purpose == "lot_entry"
    assert it.size == 30_000 // 85          # budget / (price * cv) = 352
    # opening the fill creates one confirmed ledger lot
    res = _run(sleeve.on_fill(FillEvent(
        client_order_id=it.client_order_id, symbol=SYM, side=Side.BUY,
        size=int(it.size), price=Decimal("85.0"), ts=NOW)))
    lots = sleeve.lots_open(SYM)
    assert [l["id"] for l in lots] == [res["new_lot"]]
    assert sleeve.ledger.lot(res["new_lot"])["status"] == "open"


def test_spacing_gates_add_lots(rig):
    sleeve, port, _ = rig(ACCUM_CLOSES)
    case = rig.build if False else None  # noqa: F841 (paranoia no-op)
    _run(sleeve.on_ticker(SYM, tick(85.0)))
    lot1 = _run(_fill(sleeve, port, 85.0))["new_lot"]
    assert lot1
    n_after_first = len(port.placed)
    # 87: NOT 5% below 85 -> no add
    _run(sleeve.on_ticker(SYM, tick(87.0)))
    assert len(port.placed) == n_after_first
    # 80: 80 <= 85 * 0.95 = 80.75 -> spacing ok -> add placed
    _run(sleeve.on_ticker(SYM, tick(80.0)))
    assert len(port.placed) == n_after_first + 1
    add = port.placed[-1]
    assert add.side is Side.BUY
    # fill the add -> newest open lot is the cheaper one
    _run(sleeve.on_fill(FillEvent(
        client_order_id=add.client_order_id, symbol=SYM, side=Side.BUY,
        size=int(add.size), price=Decimal("80.0"), ts=NOW)))
    newest = sleeve.newest_lot(SYM)
    assert newest is not None and abs(float(newest["entry_price"]) - 80.0) < 1e-9


# ---------------------------------------------------------------- distribute
def test_rebound_distributes_newest_lifo_lot(rig):
    sleeve, port, led = rig(REBOUND_CLOSES)
    old_id = _run(open_lot(sleeve, port, 90.0))
    new_id = _run(open_lot(sleeve, port, 95.0))
    sleeve.marks[SYM] = Ticker(symbol=SYM, mark_price=Decimal("99"),
                               best_bid=Decimal("98.4"))
    # first rebound close: sells the NEWEST (95) lot only
    _run(sleeve.on_candle(candle_at(REBOUND_CLOSES, NOW)))
    sells = sell_intents(port)
    assert len(sells) == 1
    it = sells[0]
    assert it.side is Side.SELL and it.reduce_only and it.purpose == "lifo_sell"
    assert int(it.size) == int(led.lot(new_id)["contracts"])
    # 1-per-day quota: a same-day second rebound closes nothing
    _run(sleeve.on_candle(candle_at(REBOUND_CLOSES, NOW)))
    assert len(sell_intents(port)) == 1
    # fill the sell -> newest lot closed via LIFO; the old one remains
    _run(sleeve.on_fill(FillEvent(
        client_order_id=it.client_order_id, symbol=SYM, side=Side.SELL,
        size=int(it.size), price=Decimal("98.4"), ts=NOW, reduce_only=True)))
    open_ids = [int(l["id"]) for l in sleeve.lots_open(SYM)]
    assert open_ids == [old_id] and new_id not in open_ids
    assert led.sum_signed_contracts(SYM) == int(led.lot(old_id)["contracts"])
    # a NEW day: rebound fires again and now sells the next-newest (90) lot
    _run(sleeve.on_candle(candle_at(REBOUND_CLOSES, DAY2)))
    sell2 = sell_intents(port)
    assert len(sell2) == 2
    assert int(sell2[1].size) == int(led.lot(old_id)["contracts"])


def test_distribute_respects_min_profit_gate(rig):
    sleeve, port, _ = rig(REBOUND_CLOSES)
    _run(open_lot(sleeve, port, 95.0))
    sleeve.marks[SYM] = Ticker(symbol=SYM, mark_price=Decimal("96"),
                               best_bid=Decimal("96"))
    # 96 vs entry 95 = 1.05% < min_lot_profit_pct 3% -> NO sell order
    _run(sleeve.on_candle(candle_at(REBOUND_CLOSES, NOW, price=96.0)))
    assert sell_intents(port) == []
    # crossing 3% produces the sell
    _run(sleeve.on_candle(candle_at(REBOUND_CLOSES, NOW, price=98.5)))
    assert len(sell_intents(port)) == 1


# ----------------------------------------------------------- per-symbol stop
def test_symbol_stop_sets_distribute_only(rig):
    sleeve, port, _ = rig(ACCUM_CLOSES)
    _run(open_lot(sleeve, port, 90.0))
    placed_before = len(port.placed)          # entry + its resting TP
    # avg cost 90 -> price 67 is -25.6% from avg >= 25% -> DISTRIBUTE_ONLY
    _run(sleeve.on_candle(candle_at(ACCUM_CLOSES, NOW, price=67.0)))
    assert sleeve.symbol_status[SYM] == "DISTRIBUTE_ONLY"
    # and ticker entries are refused from then on
    _run(sleeve.on_ticker(SYM, tick(67.0)))
    assert len(port.placed) == placed_before


# ------------------------------------------------------- spread + blackout
def test_spread_time_table_and_earnings_blackout(rig, tmp_path):
    csvp = tmp_path / "earnings.csv"
    csvp.write_text("AAPL,2026-06-02\n")
    sleeve, port, _ = rig(ACCUM_CLOSES, earnings_blackout_csv=str(csvp))
    # inside 13:30-20:00 UTC vs closed session widens differently
    in_h = sleeve.spread_cap(datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc))
    out_h = sleeve.spread_cap(datetime(2026, 6, 1, 22, 0, tzinfo=timezone.utc))
    assert in_h == pytest.approx(0.01)      # us_market_hours multiplier 1.0
    assert out_h > in_h                     # closed sessions are widened
    # blackout: earnings 2026-06-02, +/- 2 days blocked
    assert sleeve.earnings_blackout(SYM, NOW) is True
    assert sleeve.earnings_blackout(
        SYM, datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
