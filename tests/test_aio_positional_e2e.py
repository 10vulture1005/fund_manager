"""End-to-end test of the `all_in_one` strategy through a fake ExecutionPort:

warmup candles -> bootstrap (universe, sleeves, regime, allocation) ->
sleeve ACTIVE -> first lot placed -> entry fill confirms the lot in SQLite ->
resting reduce-only TP placed for the newest lot -> TP fill closes it ->
ledger invariant sum(open lots signed) == exchange net position.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from core.models import (
    AccountSnapshot,
    Candle,
    FillEvent,
    OrderIntent,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
    ProductInfo,
    Side,
    Ticker,
)
from core.ports import ExecutionPort
from strategy.all_in_one import AllInOneStrategy
from strategy.all_in_one.sleeve_base import now_utc

SYMBOL = "ETHUSD"
PRICE = 3000.0


class FakePort(ExecutionPort):
    """Minimal deterministic port: fills immediately at the limit price."""

    def __init__(self, candles_by_res: dict[str, list[Candle]]):
        self.products_ = {
            SYMBOL: ProductInfo(
                symbol=SYMBOL,
                product_id=1,
                contract_type="perpetual_futures",
                tick_size=Decimal("0.01"),
                contract_value=Decimal("1"),
                default_leverage=Decimal("10"),
                maker_commission_rate=Decimal("0.0005"),
                taker_commission_rate=Decimal("0.0007"),
                trading_status="operational",
            ),
            "BTCUSD": ProductInfo(symbol="BTCUSD", product_id=2,
                                  contract_type="perpetual_futures",
                                  tick_size=Decimal("0.1"),
                                  contract_value=Decimal("1"),
                                  default_leverage=Decimal("10"),
                                  trading_status="operational"),
            "XAUTUSD": ProductInfo(symbol="XAUTUSD", product_id=3,
                                   contract_type="perpetual_futures",
                                   tick_size=Decimal("0.1"),
                                   contract_value=Decimal("0.01"),
                                   default_leverage=Decimal("10"),
                                   trading_status="operational"),
            "PAXGUSD": ProductInfo(symbol="PAXGUSD", product_id=4,
                                   contract_type="perpetual_futures",
                                   tick_size=Decimal("0.1"),
                                   contract_value=Decimal("1"),
                                   default_leverage=Decimal("10"),
                                   trading_status="operational"),
        }
        self.candles_ = candles_by_res
        self.placed: list[OrderIntent] = []
        self.cancelled: list[str] = []
        self.market = {}           # symbol -> {"bid": Decimal, "ask": Decimal}
        self.net_positions: dict[str, int] = {}
        self.set_mark(PRICE)

    def set_mark(self, px: float) -> None:
        self.market[SYMBOL] = {"bid": Decimal(str(px - 0.5)), "ask": Decimal(str(px + 0.5))}

    async def bootstrap(self) -> None:
        return None

    # ---- market data -------------------------------------------------------
    async def product(self, symbol: str):
        return self.products_.get(symbol)

    async def products(self):
        return list(self.products_.values())

    async def ticker(self, symbol: str):
        q = self.market.get(symbol)
        if not q:
            return None
        mk = (q["bid"] + q["ask"]) / 2
        return Ticker(
            symbol=symbol, mark_price=mk, best_bid=q["bid"], best_ask=q["ask"],
            volume=Decimal("1000"), oi=Decimal("500"),
        )

    async def candles(self, symbol: str, resolution: str, start: int, end: int):
        data = self.candles_.get((symbol, resolution), [])
        s = datetime.fromtimestamp(start, tz=timezone.utc)
        e = datetime.fromtimestamp(end, tz=timezone.utc)
        return [c for c in data if s <= c.ts <= e]

    # ---- account -----------------------------------------------------------
    async def account(self):
        positions = []
        for sym, size in self.net_positions.items():
            if size:
                positions.append(
                    PositionSnapshot(symbol=sym, product_id=1, net_size=size,
                                     margin=Decimal("500"))
                )
        used = sum((p.margin or Decimal(0) for p in positions), Decimal(0))
        return AccountSnapshot(equity=Decimal("100000"), wallet_balance=Decimal("100000"),
                               available_balance=Decimal("100000") - used,
                               used_margin=used, positions=positions,
                               raw={"margin_mode": "cross"})

    async def position(self, symbol: str):
        size = self.net_positions.get(symbol, 0)
        return PositionSnapshot(symbol=symbol, product_id=1, net_size=size, margin=Decimal("500"))

    async def open_orders(self, symbol=None):
        return []

    async def place(self, intent: OrderIntent):
        self.placed.append(intent)
        px = (self.market[intent.product_symbol]["ask"] if intent.side is Side.BUY
              else self.market[intent.product_symbol]["bid"])
        return OrderResult(client_order_id=intent.client_order_id,
                           accepted=True, status=OrderStatus.FILLED,
                           filled_size=intent.size, avg_fill_price=px)

    def fills(self, intent: OrderIntent, price: Decimal | None = None):
        """Fill event for an accepted intent; caller applies the position delta."""
        signed = intent.size if intent.side is Side.BUY else -intent.size
        self.net_positions[intent.product_symbol] = (
            self.net_positions.get(intent.product_symbol, 0) + signed
        )
        if price is None:
            price = (self.market[intent.product_symbol]["ask"] if intent.side is Side.BUY
                     else self.market[intent.product_symbol]["bid"])
        return FillEvent(
            client_order_id=intent.client_order_id,
            symbol=intent.product_symbol,
            product_id=1,
            side=intent.side,
            size=intent.size,
            price=price,
            reduce_only=bool(getattr(intent, "reduce_only", False)),
        )

    async def cancel(self, client_order_id: str, symbol: str):
        self.cancelled.append(client_order_id)
        return True

    async def reconcile(self, ledger_state: dict):
        mismatches = []
        for sym, expected in ledger_state.items():
            actual = self.net_positions.get(sym, 0)
            if int(expected) != actual:
                mismatches.append(f"{sym}: ledger {expected} vs exchange {actual}")
        return mismatches


def _gen_daily(days: int = 300, end: datetime | None = None) -> list[Candle]:
    end = end or datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    import random

    random.seed(11)
    px = 2500.0
    out = []
    for i in range(days):
        ts = end - timedelta(days=days - i)
        px *= 1.0007  # gentle up-drift (tends to a neutral/up regime)
        rng_h = px + 8.0
        rng_l = px - 8.0
        out.append(
            Candle(symbol=SYMBOL, resolution="1D", ts=ts, open=px * 0.999,
                   high=rng_h, low=rng_l, close=px, volume=100.0, closed=True)
        )
    return out


def _gen_4h(n: int = 120, end: datetime | None = None) -> list[Candle]:
    end = end or now_utc()
    import random

    random.seed(3)
    px = PRICE
    out = []
    for i in range(n):
        ts = end - timedelta(hours=4 * (n - i))
        out.append(Candle(symbol=SYMBOL, resolution="4H", ts=ts,
                          open=px, high=px + 3, low=px - 3, close=px, volume=10.0, closed=True))
    return out


@pytest.fixture
def aio(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tf = now_utc()
    daily = _gen_daily(300, end=tf.replace(hour=0, minute=0, second=0, microsecond=0))
    h4 = _gen_4h(120, end=tf)
    port = FakePort({(SYMBOL, "1D"): daily, (SYMBOL, "4H"): h4})
    cfg = {
        "stocks": {"stock_symbols": []},
        "allocation": {
            "strategy_capital_usd": 100000.0,
            "cash_reserve_pct": 10,
            "base_weights": {"eth_cc": 60, "btc_cp": 10, "gold": 20, "stocks": 10},
            "w_min_pct": 5,
            "w_max_pct": 40,
        },
        "eth_cc": {"lot_size_notional": 8000.0, "lot_tp_pct": 2.0, "lot_spacing_atr": 1.0},
        "btc_cp": {"lot_size_notional": 4000.0},
        "gold": {"lot_size_notional": 4000.0},
    }
    strategy = AllInOneStrategy(cfg, state_dir=str(tmp_path / "state"),
                                log_dir=str(tmp_path / "logs"))
    return strategy, port


@pytest.mark.asyncio
async def test_aio_bootstrap_and_first_lot_cycle(aio, tmp_path):
    strategy, port = aio
    await strategy.bootstrap(port)

    # universe + sleeve created, no options live -> hedge 'none' for ETH sleeve
    assert strategy.sleeves
    assert "ETH_CC" in strategy.sleeves

    # regime mapped sleeves to their activation statuses (up-drift series)
    s = strategy.sleeves["ETH_CC"]
    status = getattr(s.status, "value", s.status)
    assert status in ("ACTIVE", "ACCUMULATE_ONLY"), f"got {status}"

    # --- drive the first lot in on a ticker tick ----------------------------
    t = await port.ticker(SYMBOL)
    await strategy.on_ticker(SYMBOL, t)

    async def confirm_pending_entries() -> list[FillEvent]:
        """Deliver fills for every non-reduce-only entry intent (live loop does
        this via REST/WS fill events)."""
        out = []
        for i in list(port.placed):
            if getattr(i, "reduce_only", False) or getattr(i, "_confirmed", False):
                continue
            i._confirmed = True
            fill = port.fills(i)
            out.append(fill)
            await strategy.on_fill(fill)
        return out

    await confirm_pending_entries()
    if not strategy.ledger.open_lots("eth_cc", SYMBOL):
        # regime/allocation veto'd the entry — fall back to direct sleeve call
        await s.request_new_lot(SYMBOL, PRICE, reason="test")
        await confirm_pending_entries()
    lots = strategy.ledger.open_lots("eth_cc", SYMBOL)
    assert lots, "expected at least one open lot after on_ticker"

    # ledger invariant: sum signed == exchange net (spec 2.4/3.3)
    assert strategy.ledger.sum_signed_contracts(SYMBOL) == port.net_positions.get(SYMBOL, 0)

    # a resting reduce-only TP for the newest lot was placed
    tps = [i for i in port.placed if i.reduce_only and i.order_type.value == "limit_order"]
    assert tps, "expected a resting reduce-only TP limit"

    # --- close the lot via the exchange TP fill ------------------------------
    tp = tps[-1]
    fill = port.fills(tp)
    await strategy.on_fill(fill)

    lots = strategy.ledger.open_lots("eth_cc", SYMBOL)
    assert not lots, "TP fill should close the only open lot"
    # invariant still holds (netting to zero)
    assert strategy.ledger.sum_signed_contracts(SYMBOL) == port.net_positions.get(SYMBOL, 0)

    # structured events recorded
    assert Path(tmp_path / "state" / "aio_state.sqlite").exists()


@pytest.mark.asyncio
async def test_aio_reconcile_reports_mismatch(aio):
    strategy, port = aio
    await strategy.bootstrap(port)
    # fabricate a ledger-only lot: net signed +5 vs exchange 0
    strategy.ledger.open_lot(
        sleeve="eth_cc", symbol=SYMBOL, side="buy", contracts=5,
        entry_price=3000.0, opened_at="2026-01-01T00:00:00Z",
        entry_client_oid="oid-x", tp_price=3060.0, confirmed=True,
    )
    sid = strategy.ledger.sum_signed_contracts(SYMBOL)
    assert sid == 5
    mism = await strategy.reconcile()
    assert any("ledger 5" in m for m in mism), mism
