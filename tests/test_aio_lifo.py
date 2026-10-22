"""test_aio_lifo — strict LIFO ledger + TP re-placement for the newest lot
(spec 2.4). Uses the shared core ledger and a minimal fake ExecutionPort."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

import pytest

from core.ledger import LedgerError, LIFOLedger
from strategy.all_in_one import DEFAULTS
from strategy.all_in_one.regime import HistoryStore
from strategy.all_in_one.sleeve_base import SleeveBase, SleeveStatus
from core.models import Candle, Ticker


class FakeExecPort:
    """Order sink: records placed orders; never fills by itself."""

    def __init__(self) -> None:
        self.placed: list[Any] = []
        self.accept_everything = True

    async def product(self, symbol):
        from core.models import ProductInfo
        if symbol == "ETHUSD":
            return ProductInfo(symbol="ETHUSD", product_id=1,
                               contract_type="perpetual_futures",
                               tick_size=Decimal("0.01"), contract_value=Decimal("1"))
        return None

    async def place(self, intent):
        from core.models import OrderResult, OrderStatus
        if not self.accept_everything:
            return OrderResult(client_order_id=intent.client_order_id,
                               accepted=False, error_code="insufficient_margin")
        self.placed.append(intent)
        return OrderResult(client_order_id=intent.client_order_id, accepted=True,
                           order_id="x1", status=OrderStatus.OPEN)
    async def cancel(self, client_order_id, symbol):
        return True


class _Sleeve(SleeveBase):
    name = "eth_cc"


@pytest.fixture()
def ledger(tmp_path):
    led = LIFOLedger(str(tmp_path / "ledge.db"), prefix="")
    yield led
    led.close()


# --------------------------------------------------------------------- ledger
def ledger_open(led: LIFOLedger, i: int, side: str = "buy") -> int:
    return led.open_lot("eth_cc", "ETHUSD", side, 1, 100.0 + i,
                        tp_price=102.0 + i, entry_client_oid=f"e{i}")


def test_strict_lifo_only_newest_readable_then_next(ledger: LIFOLedger):
    a = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 100.0,
                        entry_client_oid="eA")
    b = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 110.0,
                        entry_client_oid="eB")
    c = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 120.0,
                        entry_client_oid="eC")
    # open lots A, B, C (newest = C): the only readable/eligible lot is C
    newest = ledger.newest_open_lot("eth_cc", "ETHUSD")
    assert newest["id"] == c
    lots = ledger.open_lots("eth_cc", "ETHUSD")
    assert [l["id"] for l in lots] == [a, b, c]     # oldest -> newest
    # closing an older while a newer is open is a strict-LIFO violation
    with pytest.raises(LedgerError):
        ledger.close_lot(b, 111.0, exit_client_oid="x")
    with pytest.raises(LedgerError):
        ledger.close_lot(a, 101.0, exit_client_oid="x")
    # close C -> B becomes eligible, then A
    ledger.close_lot(c, 122.0, exit_client_oid="xC")
    ledger.close_lot(b, 112.0, exit_client_oid="xB")
    ledger.close_lot(a, 102.0, exit_client_oid="xA")
    assert ledger.open_lots("eth_cc", "ETHUSD") == []


def test_close_lot_enforces_strict_by_default(ledger: LIFOLedger):
    a = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 100.0)
    b = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 110.0)
    with pytest.raises(LedgerError):
        ledger.close_lot(a, 101.0)
    ledger.close_lot(b, 111.0)             # newest first — allowed
    ledger.close_lot(a, 101.0)             # then the older one


def test_open_lots_invariant_signed_sum_is_exchange_position(ledger: LIFOLedger):
    ledger.open_lot("eth_cc", "ETHUSD", "buy", 3, 100.0)
    ledger.open_lot("eth_cc", "ETHUSD", "buy", 2, 110.0)
    ledger.close_lot(ledger.newest_open_lot("eth_cc", "ETHUSD")["id"], 120.0)
    ledger.open_lot("btc_cp", "BTCUSD", "sell", 4, 20000.0)
    # sum of open lots (signed) per symbol == the exchange net position
    assert ledger.sum_signed_contracts("ETHUSD") == 3
    assert ledger.sum_signed_contracts("BTCUSD") == -4
    assert ledger.sum_signed_contracts("ETHUSD", sleeve="btc_cp") == 0
    assert ledger.sum_signed_contracts("NOSUCH") == 0


def test_tp_price_survives_in_lots_table(ledger: LIFOLedger):
    ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 100.0, tp_price=102.0)
    lot = ledger.newest_open_lot("eth_cc", "ETHUSD")
    assert lot["tp_price"] == pytest.approx(102.0)


# --------------------------------------------------------------------- sleeve
def _sleeve(ledger: LIFOLedger, status: str = "ACTIVE") -> tuple[_Sleeve, FakeExecPort]:
    exec_port = FakeExecPort()
    s = _Sleeve("eth_cc", ["ETHUSD"], exec_port, ledger,
                _FakeEvents(), dict(DEFAULTS), HistoryStore())
    s.set_status(status, "test")
    return s, exec_port


class _FakeEvents:
    def log_note(self, note):
        return 1

    def log(self, **kw):
        return 1


async def test_exactly_one_resting_tp_for_newest_lot(ledger: LIFOLedger):
    s, port = _sleeve(ledger)
    ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 3000.0, tp_price=3060.0)
    oid_1 = await s.ensure_resting_tp("ETHUSD")      # places ONE reduce-only TP
    assert oid_1 is not None
    placed_tp = [o for o in port.placed if o.purpose == "lot_tp"]
    assert len(placed_tp) == 1
    assert float(placed_tp[0].limit_price) == pytest.approx(3060.0, abs=0.01)
    assert placed_tp[0].reduce_only is True
    # calling again is idempotent: still exactly ONE resting TP
    oid_2 = await s.ensure_resting_tp("ETHUSD")
    assert oid_2 == oid_1
    assert len([o for o in port.placed if o.purpose == "lot_tp"]) == 1


async def test_tp_replaced_for_next_newest_after_close(ledger: LIFOLedger):
    s, port = _sleeve(ledger)
    a = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 2900.0, tp_price=2958.0)
    b = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 3000.0, tp_price=3060.0)
    oid_tp_b = await s.ensure_resting_tp("ETHUSD")
    # simulate the newest lot (b) closing via its TP fill:
    ledger.close_lot(b, 3060.0, exit_client_oid=oid_tp_b)
    n_before = len([o for o in port.placed if o.purpose == "lot_tp"])
    await s.after_lot_closed("ETHUSD", b)
    newer = [o for o in port.placed if o.purpose == "lot_tp"][n_before:]
    assert len(newer) == 1
    # TP for the surviving (next-newest) lot A at ITS stored tp_price 2958
    assert float(newer[0].limit_price) == pytest.approx(2958.0, abs=0.02)
    assert int(newer[0].meta["lot_id"]) == a


async def test_software_tp_fallback_market_closes_when_no_resting(
        ledger: LIFOLedger):
    s, port = _sleeve(ledger)
    ledger.open_lot("eth_cc", "ETHUSD", "buy", 2, 3000.0, tp_price=3060.0)
    # no resting TP yet: mark trades through tp -> software fallback closes it
    s.marks["ETHUSD"] = Ticker(symbol="ETHUSD", mark_price=Decimal("3061"))
    await s.tp_fallback("ETHUSD")
    exits = [o for o in port.placed if o.order_type.value == "market_order"]
    assert len(exits) == 1
    assert exits[0].reduce_only is True
    assert exits[0].size == 2
