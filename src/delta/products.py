"""Product discovery and catalog (spec 1.6).

On startup the bot walks `GET /products?states=live` fully (paginated,
never hard-coded ids) and resolves every configured symbol into a
`core.models.ProductInfo`: tick size, contract value, contract type, maker/
taker rates, trading status, product_specs (only_reduce_only_orders_allowed,
tags), price band and — for options — strike price / expiry.

Symbols that are missing or not `operational` are surfaced with is_live=False
metadata so callers can skip-and-log while the rest of the book keeps running.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .rest import DeltaRestClient
from core.models import ProductInfo

logger = logging.getLogger(__name__)

RWA_SYMBOLS = ("XAUTUSD", "PAXGUSD", "SLVONUSD")
_ENERGY_RE = re.compile(r"oil|gas|crude|energy|barrel", re.IGNORECASE)
_STOCK_PERP_RE = re.compile(r"^[A-Z]{1,7}XUSD$")  # e.g. NVDAXUSD, TSLAXUSD, AAPLXUSD
# TODO(VERIFY): the "stock perp" naming pattern and category/tags live only in
# the prompt; confirm exact identification rule (category value / tags) in docs
# or /products payloads before relying on it.


def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


_NUM_PREFIX_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")


def _dec_prefix(value: Any, default: Decimal = Decimal("1")) -> Decimal:
    """Decimal parsed from a string that may carry unit suffixes, e.g.
    contract_value `"0.01 XAU"` → 0.01 (docs send units inside the string).
    TODO(VERIFY): confirm the exact /products contract_value format.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float, Decimal)):
        return _dec(value, default)
    match = _NUM_PREFIX_RE.match(str(value))
    if not match:
        return default
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return default


def _price_band(item: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """price_band high/low; accept the key spellings seen in the wild
    (low/lower and high/upper) TODO(VERIFY) exact field names.
    """
    band = item.get("price_band") or {}
    if not isinstance(band, dict):
        return None, None
    low = band.get("low", band.get("lower", band.get("lower_limit")))
    high = band.get("high", band.get("upper", band.get("upper_limit")))
    return (
        _dec(low, Decimal("-1")) if low is not None else None,
        _dec(high, Decimal("-1")) if high is not None else None,
    )


def product_info_from_api(item: dict[str, Any]) -> ProductInfo:
    """Parse one /products payload dict into ProductInfo (spec 1.3: big
    decimals arrive as strings)."""
    specs = item.get("product_specs") or {}
    underlying = item.get("underlying_asset") or {}
    band_low, band_high = _price_band(item)
    return ProductInfo(
        symbol=str(item.get("symbol", "")),
        product_id=int(item.get("id", item.get("product_id", 0))),
        contract_type=str(item.get("contract_type", "")),
        tick_size=_dec(item.get("tick_size"), Decimal("0.0001")),
        contract_value=_dec_prefix(item.get("contract_value"), Decimal("1")),
        contract_unit_currency=str(item.get("contract_unit_currency", "") or ""),
        default_leverage=_dec(item.get("default_leverage"), Decimal("0")),
        max_leverage_notional=(
            _dec(item.get("max_leverage_notional"), Decimal("0"))
            if item.get("max_leverage_notional") is not None
            else None
        ),
        position_size_limit=(
            int(position_limit) if (position_limit := item.get("position_size_limit")) else None
        ),
        maker_commission_rate=_dec(item.get("maker_commission_rate")),
        taker_commission_rate=_dec(item.get("taker_commission_rate")),
        trading_status=str(item.get("trading_status", "unknown")),
        only_reduce_only_orders_allowed=bool(specs.get("only_reduce_only_orders_allowed", False)),
        tags=list(specs.get("tags") or []),
        price_band_low=band_low,
        price_band_high=band_high,
        strike_price=_dec(item["strike_price"]) if item.get("strike_price") not in (None, "") else None,
        expiry_date=item.get("expiry_date"),
        underlying_asset_symbol=underlying.get("symbol"),
        description=str(item.get("description", "") or ""),
        raw=dict(item),
    )


@dataclass
class ResolvedProduct:
    """A configured symbol's resolution result — callers skip-and-log on
    is_live=False (spec 1.6)."""

    symbol: str
    product: ProductInfo | None
    is_live: bool
    reason: str = "ok"  # ok | missing | not_operational | only_reduce_only


class ProductCatalog:
    """Symbol- and id-indexed cache over a fully-paginated /products walk."""

    def __init__(self, rest: DeltaRestClient | None = None) -> None:
        self._rest = rest
        self._by_symbol: dict[str, ProductInfo] = {}
        self._by_id: dict[int, ProductInfo] = {}

    # ---------------------------------------------------------------- loaders
    async def load(self, states: str = "live") -> list[ProductInfo]:
        assert self._rest is not None, "ProductCatalog needs a DeltaRestClient"
        items = await self._rest.get_products(states=states)
        infos = [product_info_from_api(i) for i in items]
        self._by_symbol = {i.symbol: i for i in infos}
        self._by_id = {i.product_id: i for i in infos}
        logger.info("product catalog loaded: %s live products", len(infos))
        return infos

    def prime_with(self, items: Iterable[dict[str, Any]]) -> list[ProductInfo]:
        """Inject canned payloads (tests, offline backtests)."""
        infos = [product_info_from_api(i) for i in items]
        for info in infos:
            self._by_symbol[info.symbol] = info
            self._by_id[info.product_id] = info
        return list(infos)

    # -------------------------------------------------------------- accessors
    def get(self, symbol: str) -> ProductInfo | None:
        return self._by_symbol.get(symbol)

    def get_by_id(self, product_id: int) -> ProductInfo | None:
        return self._by_id.get(product_id)

    def all(self) -> list[ProductInfo]:
        return list(self._by_symbol.values())

    # ---------------------------------------------------------- resolution
    def resolve(self, symbol: str) -> ResolvedProduct:
        product = self._by_symbol.get(symbol)
        if product is None:
            return ResolvedProduct(symbol, None, is_live=False, reason="missing")
        if not product.is_perp and product.contract_type not in (
            "futures", "perpetual_futures", "call_options", "put_options",
            "interest_rate_swap",
        ):
            return ResolvedProduct(symbol, product, is_live=False, reason="unsupported_type")
        if product.trading_status != "operational":
            return ResolvedProduct(
                symbol, product, is_live=False,
                reason=f"not_operational({product.trading_status})",
            )
        return ResolvedProduct(symbol, product, is_live=True, reason="ok")

    def resolve_symbols(self, symbols: Iterable[str]) -> list[ResolvedProduct]:
        return [self.resolve(s) for s in symbols]

    def resolve_rwa_symbols(self, configured: Iterable[str] | None = None) -> list[ResolvedProduct]:
        """Default RWA universe per spec 2.2: XAUTUSD, PAXGUSD, SLVONUSD."""
        symbols = list(configured) if configured else list(RWA_SYMBOLS)
        return self.resolve_symbols(symbols)

    def scan_energy_symbols(self) -> list[ResolvedProduct]:
        """Find live energy/oil/gas perps by matching description /
        short_description / tags (spec 2.2: categories are not guaranteed).
        """
        out: list[ResolvedProduct] = []
        for info in self.all():
            hay = " ".join(
                [
                    info.description,
                    str(info.raw.get("short_description", "") or ""),
                    " ".join(info.tags),
                ]
            )
            if _ENERGY_RE.search(hay) and info.is_perp:
                out.append(self.resolve(info.symbol))
        return out

    def stock_perp_symbols(self) -> list[ResolvedProduct]:
        """Live US-equity stock perps (symbols like NVDAXUSD — spec 1.6).
        TODO(VERIFY): match on tags/category when the exact values are known.
        """
        out: list[ResolvedProduct] = []
        for info in self.all():
            if not _STOCK_PERP_RE.match(info.symbol):
                lowered = {str(t).lower() for t in info.tags}
                if not ({"stocks", "us_stocks", "equities"} & lowered):
                    continue
            if info.is_perp:
                out.append(self.resolve(info.symbol))
        return out
