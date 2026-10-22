"""test_aio_portfolio — global breach logic (halt + de-risk newest-first),
allocation budgets, cross-margin reaction (spec 2.3 / 2.10)."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.events import EventLogger
from core.ledger import LIFOLedger
from core.models import AccountSnapshot, PositionSnapshot
from core.state import DeltaSchema
from strategy.all_in_one import DEFAULTS
from strategy.all_in_one.allocation import Allocator, SLEEVES
from strategy.all_in_one.portfolio import PortfolioManager

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def pm(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["allocation"] = {**copy.deepcopy(DEFAULTS["allocation"])}
    cfg["allocation"]["strategy_capital_usd"] = 100_000.0
    cfg["allocation"]["base_weights"] = {"eth_cc": 0.30, "btc_cp": 0.20,
                                         "gold": 0.25, "stocks": 0.25}
    db = DeltaSchema(str(tmp_path / "events.db"))
    events = EventLogger(db, algo="aio")
    al = Allocator(cfg)
    return PortfolioManager(cfg, al, events)


def account(equity=100_000.0, used_margin=0.0, positions=None,
            margin_mode="cross") -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal(str(equity)), wallet_balance=Decimal(str(equity)),
        used_margin=Decimal(str(used_margin)),
        positions=positions or [], raw={"margin_mode": margin_mode},
    )


def pos(symbol, net, mark, liq=None, entry=None) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol, product_id=1, net_size=net,
        entry_price=Decimal(str(entry)) if entry else None,
        mark_price=Decimal(str(mark)),
        liquidation_price=Decimal(str(liq)) if liq is not None else None,
    )


# ------------------------------------------------------------- cross margin
def test_blocks_new_lots_until_cross_margin_confirmed(pm: PortfolioManager):
    assert pm.allow_new_lot("eth_cc")[0] is False       # mode unknown
    pm.set_margin_mode(False, "isolated")
    ok, why = pm.allow_new_lot("eth_cc")
    assert ok is False and "cross" in why
    pm.set_margin_mode(True, "cross")
    ok, why = pm.allow_new_lot("eth_cc")
    assert ok is True and why == "ok"


# ------------------------------------------------- gross notional / margin
def test_gross_notional_breach_halts_and_allows_derisk(pm: PortfolioManager):
    pm.set_margin_mode(True)
    # gross = 10 x 50000 = 500k > 3 x 100k equity
    st = pm.update(account(equity=100_000,
                           positions=[pos("BTCUSD", 10, 50_000.0)]), NOW)
    assert st.breach is True
    assert any("gross_notional" in r for r in st.breach_reasons)
    assert pm.risk_breached is True
    ok, why = pm.allow_new_lot("eth_cc")
    assert ok is False and "breach" in why
    # restore equity -> breach clears
    st = pm.update(account(equity=250_000,
                           positions=[pos("BTCUSD", 10, 50_000.0)]), NOW)
    assert st.breach is False
    assert pm.risk_breached is False


def test_margin_utilisation_and_liq_distance_breaches(pm: PortfolioManager):
    pm.set_margin_mode(True)
    st = pm.update(account(equity=100_000, used_margin=60_000), NOW)
    assert st.breach and any("margin utilisation" in r for r in st.breach_reasons)
    # liquidation too close: mark 3000, liq 2200 -> distance 26.6% ok;
    # mark 3000 liq 2700 -> 10% < 25% -> breach
    st2 = pm.update(account(equity=100_000, used_margin=10_000,
                            positions=[pos("ETHUSD", 1, 3_000.0, liq=2_700.0)]), NOW)
    assert st2.breach and any("liq distance" in r for r in st2.breach_reasons)
    st3 = pm.update(account(equity=100_000, used_margin=10_000,
                            positions=[pos("ETHUSD", 1, 3_000.0, liq=2_200.0)]), NOW)
    assert st3.breach is False


# ------------------------------------------------- daily loss / drawdown
def test_daily_loss_flattens_and_halts(pm: PortfolioManager):
    pm.set_margin_mode(True)
    pm.update(account(equity=100_000), NOW)             # day anchor
    later = datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc)
    st = pm.update(account(equity=96_000), later)       # -4% > 3% limit
    assert st.flatten is True
    assert st.daily_loss == pytest.approx(0.04)
    assert pm.halted is True and pm.flatten_requested is True
    ok, _ = pm.allow_new_lot("eth_cc")
    assert ok is False
    pm.clear_flatten()
    # recovery on a fresh day resets the halt
    st = pm.update(account(equity=105_000), datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc))
    assert pm.halted is False and pm.allow_new_lot("eth_cc")[0] is True


def test_drawdown_breach_flattens(pm: PortfolioManager):
    pm.set_margin_mode(True)
    pm.update(account(equity=100_000), NOW)
    st = pm.update(account(equity=80_000), NOW)         # -20% from peak > 12%
    assert st.drawdown == pytest.approx(0.20)
    assert st.flatten is True and pm.flatten_requested is True


# ----------------------------------------------------- de-risk newest-first
def test_derisk_closes_newest_lots_first(pm: PortfolioManager):
    """Breach -> de-risk: the largest-exposure sleeve closes ITS newest lot,
    one per step, until restored (spec 2.10 newest-first)."""
    pm.set_margin_mode(True)
    pm.risk_breached = True
    closed_order: list[int] = []

    class FakeSleeve:
        def __init__(self, name: str, lot_ids: list[int], per_lot: float):
            self.name = name
            self.lot_ids = list(lot_ids)          # oldest -> newest
            self.per_lot = per_lot

        def lot_exposure_usd(self) -> float:
            return self.per_lot * len(self.lot_ids)

        async def derisk_step(self) -> list[int]:
            if not self.lot_ids:
                return []
            lot_id = self.lot_ids.pop()           # NEWEST first (LIFO de-risk)
            closed_order.append(lot_id)
            return [lot_id]

    big = FakeSleeve("eth_cc", [101, 102, 103], per_lot=3000.0)
    small = FakeSleeve("btc_cp", [201, 202], per_lot=1000.0)

    async def noop() -> None:
        return None

    closed = pm_run(pm.derisk_newest_first([small, big], max_steps=3,
                                           on_done=noop))
    # step 1: big sleeve (9000 > 2000) closes its newest lot 103
    # step 2: big still 6000 > 2000 -> closes 102
    # step 3: big 3000 > small 2000 -> closes 101
    assert closed_order == [103, 102, 101]
    assert closed == [103, 102, 101]
    assert len(small.lot_ids) == 2   # smaller sleeve untouched (newest-first)


def pm_run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ------------------------------------------------------------- budgets
def test_budget_respects_allocation_and_high_vol(pm: PortfolioManager):
    pm.set_margin_mode(True)
    pm.allocator.rebalance(sleeve_trends={s: "neutral" for s in SLEEVES},
                           sleeve_vols={s: 1.0 for s in SLEEVES},
                           capital=100_000.0)
    alloc = pm.allocator.allocation()
    b_eth = pm.budget_for_new_lot("eth_cc", max_lots=5, high_vol=False)
    assert b_eth == pytest.approx(alloc.budgets["eth_cc"] / 5, rel=1e-9)
    b_eth_hv = pm.budget_for_new_lot("eth_cc", max_lots=5, high_vol=True)
    assert b_eth_hv == pytest.approx(b_eth / 2.0, rel=1e-9)
    assert "eth_cc" in pm.high_vol_sleeves              # flag marshalled
    # stocks split by symbols
    b_stocks = pm.budget_for_new_lot("stocks", max_lots=5, n_symbols=4)
    assert b_stocks == pytest.approx(alloc.budgets["stocks"] / (5 * 4), rel=1e-9)
    # breach blocks (budget is moot): allow_new_lot gates first
    pm.risk_breached = True
    ok, why = pm.allow_new_lot("eth_cc")
    assert ok is False and "breach" in why


def test_budget_blocked_when_halted(pm: PortfolioManager):
    pm.set_margin_mode(True)
    pm.halt("daily loss", flatten=True)
    ok, why = pm.allow_new_lot("eth_cc")
    assert ok is False and "halted" in why


def test_status_shape(pm: PortfolioManager):
    pm.set_margin_mode(True)
    pm.update(account(equity=100_000, positions=[
        pos("ETHUSD", 2, 3_000.0, liq=2_000.0)]), NOW)
    st = pm.status()
    assert st["margin_mode_ok"] is True
    assert st["risk"]["gross_notional"] == pytest.approx(6000.0 / 100_000.0)
    assert "weights" in st and "budgets" in st
