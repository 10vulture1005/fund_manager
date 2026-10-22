"""LIFOLedger invariants — strict LIFO, signed-contract reconciliation,
campaigns CRUD (spec 3.3 / prompt B 2.4)."""
from __future__ import annotations

import pytest

from core.ledger import LIFOLedger, LedgerError


@pytest.fixture()
def ledger(tmp_path):
    led = LIFOLedger(str(tmp_path / "ledger.db"), prefix="")
    yield led
    led.close()


class TestStrictLifo:
    def test_newest_open_lot_is_last_opened(self, ledger: LIFOLedger):
        ids = []
        for i in range(3):
            ids.append(
                ledger.open_lot("eth_cc", "ETHUSD", "buy", 2 + i, 2000.0 + i,
                                entry_client_oid=f"e{i}")
            )
        newest = ledger.newest_open_lot("eth_cc", "ETHUSD")
        assert newest["id"] == ids[-1]

    def test_close_newest_then_next_becomes_eligible(self, ledger: LIFOLedger):
        ids = [
            ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 2000.0, entry_client_oid=f"e{i}")
            for i in range(3)
        ]
        ledger.close_lot(ids[-1], price=2050.0, ts="2026-01-01T00:00:00+00:00")
        # strict: only one closed
        assert ledger.newest_open_lot("eth_cc", "ETHUSD")["id"] == ids[-2]
        rows = ledger.open_lots("eth_cc", "ETHUSD")
        assert [r["id"] for r in rows] == ids[:-1]
        # closing the next-newest is legal
        ledger.close_lot(ids[-2], price=2060.0)
        assert ledger.newest_open_lot("eth_cc", "ETHUSD")["id"] == ids[0]
        closed = ledger.lot(ids[-1])
        assert closed["status"] == "closed"
        assert closed["close_price"] == 2050.0
        assert closed["closed_at"] == "2026-01-01T00:00:00+00:00"

    def test_closing_older_lot_in_strict_mode_raises(self, ledger: LIFOLedger):
        first = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 2000.0)
        second = ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 1990.0)
        with pytest.raises(LedgerError):
            ledger.close_lot(first, price=2010.0)  # not the newest
        # explicit non-strict close is allowed (global de-risk still newest-first;
        # kill switch flatten is allowed to bypass — the CALLER decides)
        ledger.close_lot(first, price=2010.0, enforce_strict_lifo=False)
        assert ledger.lot(first)["status"] == "closed"

    def test_double_close_raises(self, ledger: LIFOLedger):
        lot = ledger.open_lot("gold", "XAUTUSD", "buy", 1, 4000.0)
        ledger.close_lot(lot, price=4010.0)
        with pytest.raises(LedgerError):
            ledger.close_lot(lot, price=4020.0)


class TestSignedContracts:
    def test_sum_matches_exchange_position(self, ledger: LIFOLedger):
        ledger.open_lot("btc_cp", "BTCUSD", "sell", 2, 60000.0)
        ledger.open_lot("btc_cp", "BTCUSD", "sell", 3, 59000.0)
        ledger.open_lot("eth_cc", "ETHUSD", "buy", 1, 3000.0)
        assert ledger.sum_signed_contracts("BTCUSD") == -5
        assert ledger.sum_signed_contracts("ETHUSD") == 1
        assert ledger.sum_signed_contracts("XAUTUSD") == 0
        # close the newest short lot (3 contracts) → sum rises to -2
        lot = ledger.newest_open_lot("btc_cp", "BTCUSD")
        ledger.close_lot(lot["id"], price=58800.0)
        assert ledger.sum_signed_contracts("BTCUSD") == -2

    def test_per_sleeve_block(self, ledger: LIFOLedger):
        ledger.open_lot("gold", "XAUTUSD", "buy", 4, 4000.0)
        ledger.open_lot("stocks", "XAUTUSD", "sell", 1, 4050.0)  # unlikely, but signed
        assert ledger.sum_signed_contracts("XAUTUSD", sleeve="gold") == 4
        assert ledger.sum_signed_contracts("XAUTUSD", sleeve="stocks") == -1


class TestOrdersAndHedges:
    def test_record_and_list(self, ledger: LIFOLedger):
        oid = ledger.record_order(
            client_order_id="fm-eth-rung1-ab12cd34", symbol="ETHUSD",
            side="buy", size=2, status="new", order_type="limit_order",
            limit_price=2000.0,
        )
        # second call with the same client_order_id must UPSERT, not insert
        oid2 = ledger.record_order(
            client_order_id="fm-eth-rung1-ab12cd34", symbol="ETHUSD",
            side="buy", size=2, filled_size=1, avg_fill_price=2001.0,
            status="filled",
        )
        assert oid == oid2
        rows = ledger.list_orders("ETHUSD", status="filled")
        assert len(rows) == 1 and rows[0]["filled_size"] == 1
        assert ledger.list_orders("ETHUSD", status="open") == []
        found = ledger.get_order("fm-eth-rung1-ab12cd34")
        assert found["status"] == "filled"
        assert ledger.get_order("missing") is None

    def test_hedges_crud(self, ledger: LIFOLedger):
        hid = ledger.open_hedge("eth_cc", "C-ETH-2800-310326", 99, "sell", 5, 90.0)
        rows = ledger.open_hedges("eth_cc")
        assert [r["id"] for r in rows] == [hid]
        ledger.close_hedge(hid, price=10.0)  # bought back
        assert ledger.open_hedges() == []
        with pytest.raises(LedgerError):
            ledger.close_hedge(hid, price=10.0)


class TestPrefixes:
    def test_prefixed_tables_are_separate(self, tmp_path):
        path = str(tmp_path / "shared.db")
        a = LIFOLedger(path, prefix="rwa_")
        b = LIFOLedger(path, prefix="ai_")
        a.open_lot("gold", "XAUTUSD", "buy", 1, 4000.0)
        a.create_campaign("XAUTUSD", "long", "ARMED")
        assert len(a.open_lots()) == 1
        assert len(b.open_lots()) == 0
        assert b.get_campaigns() == []
        assert len(a.get_campaigns()) == 1
        a.close()

    def test_prefix_rejects_bad_identifier(self, tmp_path):
        with pytest.raises(Exception):
            LIFOLedger(str(tmp_path / "x.db"), prefix="evil; DROP TABLE")


class TestCampaigns:
    def test_crud_round_trip(self, ledger: LIFOLedger):
        cid = ledger.create_campaign(
            "XAUTUSD", "long", "ARMED",
            zone={"kind": "sweep", "low": 3990.0, "high": 4005.0},
        )
        ledger.update_state(cid, "TRIGGERED")
        ledger.update_campaign(cid, entry_avg=4000.0, contracts=3,
                               tp_price=4080.0)
        armed = ledger.get_campaigns(symbol="XAUTUSD", state="TRIGGERED")
        assert len(armed) == 1 and armed[0]["id"] == cid
        assert armed[0]["zone"]["low"] == 3990.0
        assert armed[0]["entry_avg"] == 4000.0
        assert ledger.get_campaigns(state="CLOSED") == []
        ledger.close_campaign(cid, close_reason="tp_hit", ts="2026-01-02T00:00:00+00:00")
        closed = ledger.get_campaigns(state="CLOSED")
        assert len(closed) == 1
        assert closed[0]["close_reason"] == "tp_hit"
        assert closed[0]["closed_at"].startswith("2026-01-02")
