"""ProductCatalog parser tests on canned /products payloads (spec 1.6)."""
from __future__ import annotations

from decimal import Decimal

import pytest

import os
import re

from delta.products import ProductCatalog, product_info_from_api
from core.models import ProductInfo

XAUT_PERP = {
    "symbol": "XAUTUSD",
    "id": 171101,
    "contract_type": "perpetual_futures",
    "tick_size": "0.01",
    "contract_value": "0.01 XAU",  # docs sometimes suffix units; parser uses leading numeric
    "contract_unit_currency": "XAU",
    "default_leverage": "50",
    "max_leverage_notional": "30000000",
    "position_size_limit": 5000,
    "maker_commission_rate": "0.0002",
    "taker_commission_rate": "0.0005",
    "trading_status": "operational",
    "underlying_asset": {"symbol": "XAUT"},
    "price_band": {"lower": "2500.0", "upper": "5000.0"},
    "description": "Tether Gold perp (RWA metals)",
    "product_specs": {
        "tags": ["rwa", "metals"],
        "only_reduce_only_orders_allowed": False,
    },
}

CBTC_OPTION = {
    "symbol": "C-BTC-25000-310326",
    "id": 987,
    "contract_type": "call_options",
    "tick_size": "1",
    "contract_value": "0.001 BTC",
    "contract_unit_currency": "BTC",
    "maker_commission_rate": "0.0003",
    "taker_commission_rate": "0.0003",
    "trading_status": "operational",
    "underlying_asset": {"symbol": "BTC"},
    "strike_price": "25000",
    "expiry_date": "31-03-26",
    "description": "BTC call option",
    "product_specs": {"tags": ["options"]},
}

DISRUPTED_PERP = {
    **XAUT_PERP,
    "symbol": "SLVONUSD",
    "id": 2,
    "trading_status": "disrupted_cancel_only",
}


@pytest.fixture()
def catalog() -> ProductCatalog:
    c = ProductCatalog()
    c.prime_with([XAUT_PERP, CBTC_OPTION, DISRUPTED_PERP])
    return c


class TestParser:
    def test_perp_fields(self):
        info = product_info_from_api(XAUT_PERP)
        assert isinstance(info, ProductInfo)
        assert info.symbol == "XAUTUSD"
        assert info.product_id == 171101
        assert info.contract_type == "perpetual_futures"
        assert info.tick_size == Decimal("0.01")  # decimal parsed from a STRING
        assert info.contract_value > 0            # "0.01 XAU" leading numeric
        assert info.maker_commission_rate == Decimal("0.0002")
        assert info.taker_commission_rate == Decimal("0.0005")
        assert info.trading_status == "operational"
        assert info.only_reduce_only_orders_allowed is False
        assert "rwa" in info.tags
        assert info.price_band_low == Decimal("2500.0")
        assert info.price_band_high == Decimal("5000.0")
        assert info.underlying_asset_symbol == "XAUT"
        assert not info.is_option and info.is_perp

    def test_option_fields(self):
        info = product_info_from_api(CBTC_OPTION)
        assert info.is_option
        assert info.strike_price == Decimal("25000")
        assert info.expiry_date == "31-03-26"
        assert info.contract_value == Decimal("0.001")
        assert info.price_band_low is None and info.price_band_high is None

    def test_only_reduce_only_flag(self):
        item = {**XAUT_PERP, "product_specs": {"only_reduce_only_orders_allowed": True}}
        info = product_info_from_api(item)
        assert info.only_reduce_only_orders_allowed is True

    def test_contract_value_junk_units(self):
        # the numeric prefix is used when the API sends units in the string
        assert product_info_from_api(XAUT_PERP).contract_value == Decimal("0.01")


class TestCatalog:
    def test_indexes_and_get(self, catalog: ProductCatalog):
        assert catalog.get("XAUTUSD") is not None
        assert catalog.get("NOPEUSD") is None
        by_id = catalog.get_by_id(20897)
        assert by_id is None  # no such id in the canned set
        assert catalog.get_by_id(171101).symbol == "XAUTUSD"

    def test_resolve_rwa_symbols(self, catalog: ProductCatalog):
        resolved = catalog.resolve_rwa_symbols(["XAUTUSD", "PAXGUSD"])
        assert [r.symbol for r in resolved] == ["XAUTUSD", "PAXGUSD"]
        xaut = resolved[0]
        assert xaut.is_live and xaut.reason == "ok"
        missing = resolved[1]
        assert not missing.is_live and missing.reason == "missing" and missing.product is None

    def test_not_operational_surfaces_with_is_live_false(self, catalog: ProductCatalog):
        resolved = catalog.resolve("SLVONUSD")
        assert resolved.is_live is False
        assert resolved.reason == "not_operational(disrupted_cancel_only)"

    def test_scan_energy_symbols(self):
        catalog = ProductCatalog()
        catalog.prime_with(
            [
                {**XAUT_PERP, "symbol": "CLUSD", "id": 7,
                 "description": "WTI crude oil perpetual", "contract_type": "perpetual_futures"},
                CBTC_OPTION,
            ]
        )
        energies = catalog.scan_energy_symbols()
        assert [r.symbol for r in energies] == ["CLUSD"]

    def test_stock_perp_symbols(self):
        catalog = ProductCatalog()
        catalog.prime_with(
            [
                {**XAUT_PERP, "symbol": "NVDAXUSD", "id": 11},
                {**XAUT_PERP, "symbol": "TSLAXUSD", "id": 12},
                {**XAUT_PERP, "symbol": "EURUSD", "id": 13},
            ]
        )
        stocks = catalog.stock_perp_symbols()
        assert sorted(r.symbol for r in stocks) == ["NVDAXUSD", "TSLAXUSD"]
