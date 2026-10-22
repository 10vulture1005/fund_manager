"""test_aio_options — option selection (|delta - target| nearest within the
band), spread / no-bid / OI rejection, and coverage floor math capped to the
underlying exposure (spec 2.5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from strategy.all_in_one import DEFAULTS
from strategy.all_in_one.options import (
    OptionCandidate,
    OptionChain,
    coverage_contracts,
    dte_days,
    parse_expiry,
    select_option,
)

NOW = datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc)


def cand(symbol: str, kind: str, dte: float, delta: float, bid: float,
         ask: float, cv: float = 1.0, oi: float = 5000.0) -> OptionCandidate:
    return OptionCandidate(
        symbol=symbol, product_id=1, kind=kind, underlying="ETH", dte=dte,
        strike=Decimal("3000"), delta=Decimal(str(delta)), iv=Decimal("0.6"),
        bid=Decimal(str(bid)), ask=Decimal(str(ask)), oi=Decimal(str(oi)),
        contract_value=Decimal(str(cv)), expiry_date="15-06-2026",
    )


KINDS = dict(
    kind="call", target_delta=0.20, delta_lo=0.12, delta_hi=0.30,
    max_spread_pct=0.08, min_oi=100.0, dte_min=5.0, dte_max=10.0,
)


def test_selects_delta_nearest_target():
    cands = [
        cand("A", "call", 7.0, 0.15, 95, 105),
        cand("B", "call", 7.0, 0.19, 98, 104),      # |0.19-0.20| = 0.01 <- best
        cand("C", "call", 7.0, 0.28, 20, 22),
    ]
    best = select_option(cands, **KINDS, now=NOW)
    assert best is not None and best.symbol == "B"


def test_prefers_the_dte_window_and_target_over_far_delta():
    cands = [
        cand("near", "call", 14.0, 0.20, 98, 104),   # outside DTE window
        cand("win", "call", 7.0, 0.18, 98, 104),     # inside window
    ]
    best = select_option(cands, **KINDS, now=NOW)
    assert best is not None and best.symbol == "win"


def test_rejects_delta_out_of_band():
    cands = [cand("x", "call", 7.0, 0.31, 98, 104)]   # 0.31 > 0.30
    assert select_option(cands, **KINDS, now=NOW) is None


def test_rejects_wide_spread():
    cands = [cand("x", "call", 7.0, 0.20, 90, 115)]   # 25%~ mid vs ask-bid
    assert select_option(cands, **KINDS, now=NOW) is None


def test_rejects_no_positive_bid():
    c0 = cand("x", "call", 7.0, 0.20, 98, 104)
    c0.bid = None
    assert select_option([c0], **KINDS, now=NOW) is None
    c1 = cand("y", "call", 7.0, 0.20, 0, 10)          # zero bid
    assert select_option([c1], **KINDS, now=NOW) is None


def test_rejects_low_oi():
    cands = [cand("x", "call", 7.0, 0.20, 98, 104, oi=50.0)]  # < min_oi 100
    assert select_option(cands, **KINDS, now=NOW) is None


def test_rejects_wrong_kind_and_dte_out_of_window():
    cands = [
        cand("put", "put", 7.0, 0.20, 98, 104),        # wrong kind
        cand("old", "call", 2.0, 0.20, 98, 104),       # DTE < 5
        cand("far", "call", 40.0, 0.20, 98, 104),      # DTE > 10
    ]
    assert select_option(cands, **KINDS, now=NOW) is None


def test_coverage_floor_math():
    # exposure 10 coins, option contract_value 0.1, ratio 1.0 -> 100 contracts
    assert coverage_contracts(10.0, 1.0, 0.1) == 100
    # partial: exposure 10.5, cv 1 -> 10 (floored) and 10 <= 10.5 (covered)
    assert coverage_contracts(10.5, 1.0, 1.0) == 10
    assert coverage_contracts(10.5, 1.0, 1.0) * 1.0 <= 10.5
    # coverage_ratio 2.0 must STILL be capped at the raw exposure (covered!)
    assert coverage_contracts(10.0, 2.0, 1.0) == 10
    assert coverage_contracts(10.0, 2.0, 0.25) == 40   # 10/0.25 hard cap
    # zero / negative exposure -> nothing short
    assert coverage_contracts(0.0, 1.0, 1.0) == 0
    assert coverage_contracts(-5.0, 1.0, 1.0) == 0


def test_expiration_parsing_and_dte():
    assert parse_expiry("15-06-2026") == datetime(2026, 6, 15, tzinfo=timezone.utc)
    e = parse_expiry("150626")
    assert e is not None and (e.month, e.year) == (6, 2026)
    assert dte_days("12-06-2026", NOW) == pytest.approx(2.0)


def test_option_chain_fetch_via_execution_port():
    from core.models import ProductInfo, Ticker
    from strategy.all_in_one.regime import HistoryStore  # noqa: F401

    class ChainPort:
        async def products(self):
            return [
                ProductInfo(symbol="C-ETH-3000-150626", product_id=7,
                            contract_type="call_options", tick_size=Decimal("0.5"),
                            contract_value=Decimal("1"), underlying_asset_symbol="ETH",
                            strike_price=Decimal("3000"), expiry_date="15-06-2026"),
                ProductInfo(symbol="XAUTUSD", product_id=8,
                            contract_type="perpetual_futures",
                            tick_size=Decimal("0.01"), contract_value=Decimal("1")),
            ]

        async def ticker(self, symbol):
            if symbol.startswith("C-ETH"):
                return Ticker(symbol=symbol, delta=Decimal("0.19"),
                              iv=Decimal("0.6"), best_bid=Decimal("98"),
                              best_ask=Decimal("104"), oi=Decimal("5000"),
                              expiry_date="15-06-2026")
            return Ticker(symbol=symbol, mark_price=Decimal("3000"))

    chain = OptionChain(ChainPort(), dict(DEFAULTS))
    got = _run(chain.fetch("ETH", NOW))
    assert len(got) == 1
    assert got[0].kind == "call" and got[0].dte == pytest.approx(5.0)
    best = _run(chain.pick("ETH", "call", NOW))
    assert best is not None and best.symbol == "C-ETH-3000-150626"
    assert _run(chain.options_exist("ETH", NOW)) is True
    assert _run(chain.options_exist("XAUT", NOW)) is False   # RWA -> no options


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
