"""Order management for Delta Exchange India (spec 1.7).

- `place_order(OrderIntent)` is idempotent: on a timeout or ambiguous
  response it looks the order up by `client_order_id` BEFORE any retry; a
  POST /orders is never blind-retried.
- Every order gets a unique `client_order_id` shaped
  `{algo}-{symbol}-{purpose}-{uuid8}` via `core.models.new_client_order_id`.
- Pre-flight checks against product `trading_status` restrictions
  (disrupted_cancel_only / disrupted_post_only / only_reduce_only_orders_allowed,
  size limits) return explicit rejected OrderResults instead of sending.
- Bracket TP/SL upsert via `PUT /orders/bracket` to re-centre after fills.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence
from decimal import Decimal

from .rest import DeltaAPIError, DeltaRestClient
from .auth import DeltaAuthError
from core.models import (
    OrderIntent,
    OrderResult,
    OrderStatus,
    ProductInfo,
    Side,
    TimeInForce,
    OrderType,
    new_client_order_id,
)

logger = logging.getLogger(__name__)


def _dec_str(d: Decimal) -> str:
    return format(d, "f")


def off_symbol(symbol: str) -> str:
    return symbol  # passthrough alias; ids are cleaned inside new_client_order_id


class Preflight:
    """Pure checks over a ProductInfo — fully unit-testable without network."""

    @staticmethod
    def check(product: ProductInfo | None, intent: OrderIntent) -> str | None:
        """Return a rejection reason, or None when pre-flight passes (spec 1.7)."""
        if product is None:
            return "product_not_found"
        if intent.size <= 0:
            return "size_must_be_positive"
        if intent.size is not None and product.position_size_limit and intent.size > product.position_size_limit:
            return "size_limit_breached"
        if product.trading_status == "disrupted_cancel_only":
            return "trading_disrupted_cancel_only"
        if product.trading_status == "disrupted_post_only" and not (
            intent.order_type == OrderType.LIMIT and intent.post_only
        ):
            return "trading_disrupted_post_only"
        if product.only_reduce_only_orders_allowed and not intent.reduce_only:
            return "only_reduce_only_orders_allowed"
        if intent.limit_price is not None and product.price_band_low is not None and product.price_band_high is not None:
            band_low = abs(product.price_band_low) if product.price_band_low < 0 else product.price_band_low
            band_high = abs(product.price_band_high) if product.price_band_high < 0 else product.price_band_high
            if intent.limit_price < band_low or intent.limit_price > band_high:
                return "price_band_breach"
        if intent.order_type == OrderType.LIMIT and intent.limit_price is None:
            return "limit_order_missing_limit_price"
        return None


class DeltaOrderManager:
    """High-level order operations the strategies' `DeltaPort` will call."""

    def __init__(self, rest: DeltaRestClient, catalog: Any = None) -> None:
        self.rest = rest
        self.catalog = catalog  # ProductCatalog-like with .get(symbol) / .get_by_id(pid)

    # ---------------------------------------------------------------- payload
    @staticmethod
    def order_payload(intent: OrderIntent) -> dict[str, Any]:
        """Delta REST payload for one order; big decimals as strings (1.3)."""
        pid = intent.product_id
        p: dict[str, Any] = {
            "product_id": pid,
            "product_symbol": intent.product_symbol,
            "size": int(intent.size),
            "side": intent.side.value,
            "order_type": intent.order_type.value,
            "time_in_force": intent.time_in_force.value,
            "post_only": bool(intent.post_only),
            "reduce_only": bool(intent.reduce_only),
            "client_order_id": intent.client_order_id,
        }
        if intent.limit_price is not None:
            p["limit_price"] = _dec_str(intent.limit_price)
        if intent.stop_price is not None:
            p["stop_price"] = _dec_str(intent.stop_price)
        if intent.stop_order_type:
            p["stop_order_type"] = intent.stop_order_type
        if intent.stop_trigger_method:
            p["stop_trigger_method"] = intent.stop_trigger_method
        p.pop("product_symbol")
        if intent.bracket_stop_loss_price is not None:
            p["bracket_stop_loss_price"] = _dec_str(intent.bracket_stop_loss_price)
        if intent.bracket_take_profit_price is not None:
            p["bracket_take_profit_price"] = _dec_str(intent.bracket_take_profit_price)
        if intent.bracket_trail_amount is not None:
            p["bracket_trail_amount"] = _dec_str(intent.bracket_trail_amount)
        return p

    # ------------------------------------------------------------- preflight
    def _resolve(self, intent: OrderIntent) -> ProductInfo | None:
        if self.catalog is None:
            return None
        if intent.product_id and not intent.product_symbol:
            return self.catalog.get_by_id(intent.product_id)
        return self.catalog.get(intent.product_symbol)

    def preflight(self, intent: OrderIntent) -> str | None:
        return Preflight.check(self._resolve(intent), intent)

    # -------------------------------------------------------------- placement
    async def place_order(self, intent: OrderIntent) -> OrderResult:
        """Place with idempotency guards; returns an explicit OrderResult."""
        pid = intent.product_id
        if pid is None and self.catalog is not None:
            product = self._resolve(intent)
            pid = product.product_id if product else None
        reason = self.preflight(intent)
        if reason is not None:
            logger.warning("order rejected pre-flight: %s %s", intent.product_symbol, reason)
            return OrderResult(
                client_order_id=intent.client_order_id, accepted=False,
                error_code=reason, error_message=f"pre-flight: {reason}",
            )
        assert pid is not None, "product_id unknown; resolve products first"

        params = self.order_payload(intent)
        coid = params["client_order_id"]
        # spec 1.7: never blind-retry a POST; on TimeoutException look up the
        # order by client_order_id first.
        # TODO(VERIFY): exact POST /orders body enclosure — docs show the order
        # fields with bracket_* possibly under an "order" key; confirm on testnet.
        try:
            result = await self.rest.place_order(params, product_id=pid)
        except DeltaAuthError:
            raise  # auth errors must surface loudly (spec 1.2)
        except DeltaAPIError as exc:
            if not exc.transient:
                logger.error("order rejected: %s %s", exc.code, exc.message or exc.context)
                return OrderResult(
                    client_order_id=coid, accepted=False,
                    error_code=exc.code, error_message=str(exc.context or exc.message),
                    raw=exc.raw,
                )
            # ambiguous response — find the order before retrying (spec 1.7)
            # TODO(VERIFY): confirm the "lookup by client_order_id" endpoint/filter.
            lookup = await self.rest.get_order_by_client_order_id(coid, pid)
            if lookup:
                return self._result_from_order(lookup)
            return OrderResult(
                client_order_id=coid, accepted=False,
                error_code="ambiguous_timeout_no_retry",
                error_message=f"order lookup by client_order_id empty after {exc.code}; "
                "do not blind-retry the POST",
            )
        return self._result_from_order(result)

    @staticmethod
    def _result_from_order(order: dict[str, Any]) -> OrderResult:
        size = int(order.get("size", 0) or 0)
        filled = int(order.get("avg_filled_size", 0) if "avg_filled_size" in order
                     else order.get("filled_quantity", 0) or 0)
        status_map = {
            "open": OrderStatus.OPEN, "pending": OrderStatus.NEW,
            "filled": OrderStatus.FILLED, "partially_filled": OrderStatus.PARTIAL,
            "cancelled": OrderStatus.CANCELLED, "rejected": OrderStatus.REJECTED,
        }
        return OrderResult(
            client_order_id=str(order.get("client_order_id", "")),
            accepted=True,
            order_id=str(order.get("id")),
            status=status_map.get(str(order.get("state", "")).lower(), OrderStatus.NEW),
            filled_size=filled,
            avg_fill_price=(
                Decimal(str(order["average_fill_price"])) if order.get("average_fill_price") else None
            ),
            raw=dict(order),
        )

    # ---------------------------------------------------------------- cancels
    async def cancel_by_client_id(self, client_order_id: str, product_id: int) -> bool:
        """True when the order is gone (or was already gone)."""
        try:
            await self.rest.cancel_order_by_client_id(client_order_id, product_id)
            return True
        except DeltaAPIError as exc:
            if exc.code == "http_400" or str(exc.context).lower().find("not_found") >= 0:
                return True  # already gone
            raise
        # TODO(VERIFY): docs' cancel-error code for "already filled/gone" varies;
        # add the exact code here once observed on testnet.

    async def batch_cancel(
        self, client_order_ids: Sequence[str], product_id: int, batch_size: int = 50
    ) -> int:
        """Delete-by-batch in groups of max 50 (spec 1.5)."""
        total = 0
        for i in range(0, len(client_order_ids), batch_size):
            chunk = list(client_order_ids[i : i + batch_size])
            await self.rest.batch_delete_orders(product_id, client_order_ids=chunk)
            total += len(chunk)
        return total

    # ---------------------------------------------------------------- bracket
    async def bracket_upsert(
        self,
        product_id: int,
        size: int,
        stop_loss_price: Decimal | None = None,
        take_profit_price: Decimal | None = None,
        trailing_amount: Decimal | None = None,
        stop_trigger_method: str = "mark_price",
        create: bool = False,
    ) -> dict[str, Any]:
        """Create (POST) or re-centre (PUT) the single bracket for a position —
        called after every fill that changes the average entry (spec 1.7).
        TODO(VERIFY): exact bracket payload enclosure in docs (flat vs nested).
        """
        body: dict[str, Any] = {"product_id": product_id}
        if stop_loss_price is not None:
            body["bracket_stop_loss_price"] = _dec_str(stop_loss_price)
        if take_profit_price is not None:
            body["bracket_take_profit_price"] = _dec_str(take_profit_price)
        if trailing_amount is not None:
            body["bracket_trail_amount"] = _dec_str(trailing_amount)
        body["bracket_stop_trigger_method"] = stop_trigger_method
        if create:
            body["size"] = int(size)
            return await self.rest.bracket_place(product_id, int(size), body)
        return await self.rest.bracket_edit(product_id, body)
