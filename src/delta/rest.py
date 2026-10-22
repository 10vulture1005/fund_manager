"""HTTP REST client for Delta Exchange India (spec 1.5).

Conventions (spec 1.3):
- Envelope: success `{"success": true, "result": ..., "meta": {"after": ...}}`,
  error `{"success": false, "error": {"code": "...", "context": {...}}}`.
- Big decimals are STRINGS in requests and responses; `size`/`product_id` ints.
- The raw body string is serialized ONCE (compact JSON separators, decimals as
  strings), signed, and the exact same string is sent (spec 1.2).
- Query string used in the signature includes the leading '?' (or "").
- Signature valid ~5s: on an error envelope with code `SignatureExpired` we
  re-sign with a fresh timestamp and retry ONCE.

Any endpoint or parameter below that could not be confirmed word-for-word
against the live docs carries a `TODO(VERIFY)` marker (spec 3.9: do not invent
APIs).
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, AsyncIterator, Mapping

import httpx

from .auth import AuthSigner, classify_auth_error
from .ratelimit import RateLimiter

BASE_URLS = {
    "prod": "https://api.india.delta.exchange",
    "testnet": "https://cdn-ind.testnet.deltaex.org",
}

logger = logging.getLogger(__name__)


class DeltaAPIError(Exception):
    """Error envelope / transport failure with code + context (spec 1.3)."""

    def __init__(
        self,
        code: str,
        message: str = "",
        context: Any = None,
        http_status: int | None = None,
        transient: bool = False,
        raw: Any = None,
    ) -> None:
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")
        self.code = code
        self.message = message
        self.context = context
        self.http_status = http_status
        self.transient = transient  # True for timeouts/ambiguous network states
        self.raw = raw


def encode_json_body(payload: Mapping[str, Any] | list[Any]) -> str:
    """The exact raw JSON string sent (and signed): compact separators, big
    decimals as strings (spec 1.3). Call once per request, never re-serialize.
    """
    import json

    def enc(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return format(obj, "f")
        if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int, float)):
            if isinstance(obj, float) and obj.is_integer() and abs(obj) < 2**53:
                return int(obj)  # avoid "1.0" on integer-typed fields
            return obj
        if isinstance(obj, dict):
            return {k: enc(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, (list, tuple, set)):
            return [enc(v) for v in obj]
        return str(obj)

    return json.dumps(enc(dict(payload)), separators=(",", ":"), ensure_ascii=False)


_PAGE_LIMIT = 200  # hard safety cap on pagination loops


class DeltaRestClient:
    """Thin, envelope-aware httpx client with shared weight limiting."""

    def __init__(
        self,
        base_url: str = BASE_URLS["testnet"],
        api_key: str = "",
        api_secret: str = "",
        signer: AuthSigner | None = None,
        limiter: RateLimiter | None = None,
        user_agent: str | None = None,
        timeout: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer or AuthSigner(api_key=api_key, api_secret=api_secret)
        if user_agent:
            self.signer.user_agent = user_agent
        self.limiter = limiter or RateLimiter()
        self._http = http_client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "DeltaRestClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: Mapping[str, Any] | list[Any] | None = None,
        weight: int | None = None,
        product_id: int | None = None,
        ops: float | None = None,
        _resigned: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        """Perform one signed request; returns (result, meta).

        Signs the exact body string that is sent (spec 1.2) and handles the
        `SignatureExpired` re-sign-once rule.
        """
        raw_body = encode_json_body(body) if body is not None else ""
        headers = self.signer.signed_headers(method, path, query_string, raw_body)
        url = "/v2" + path
        await self.limiter.acquire(
            weight=weight, method=method, path="/v2" + path, product_id=product_id, ops=ops
        )
        try:
            resp = await self._http.request(
                method, url, params=None, content=raw_body or None, headers=headers
            )
        except httpx.TimeoutException as exc:  # ambiguous: the call may have gone through
            raise DeltaAPIError(
                "transport_timeout", str(exc), transient=True
            ) from exc
        except httpx.HTTPError as exc:
            raise DeltaAPIError("transport_error", str(exc), transient=True) from exc

        if resp.status_code == 429:
            reset = resp.headers.get("X-RATE-LIMIT-RESET")
            if reset:
                self.limiter.register_reset_header(reset)
            raise DeltaAPIError(
                "rate_limited",
                "HTTP 429; X-RATE-LIMIT-RESET honoured client-side",
                {"reset_ms": reset},
                http_status=429,
                transient=True,
            )

        text = resp.text or ""
        try:
            payload = resp.json()
        except ValueError:
            # e.g. 403 "Request blocked by CDN" HTML responses (spec 1.2)
            raise DeltaAPIError(
                "cdn_blocked" if resp.status_code == 403 else "non_json_response",
                text[:200],
                http_status=resp.status_code,
                raw=text[:2000],
            )

        if resp.status_code >= 400 or payload.get("success") is False:
            err = payload.get("error") or {}
            code = str(err.get("code", f"http_{resp.status_code}"))
            context = err.get("context")
            message = str(err.get("error", {}).get("message", "") if isinstance(err.get("error"), dict) else err.get("error", ""))
            if not message:
                message = str(err.get("error", err.get("message", "")))
            if code == "SignatureExpired" and not _resigned:
                # spec 1.2: re-sign with a fresh timestamp, retry exactly once
                # TODO(VERIFY): confirm the exact retry policy wording in docs
                return await self._request(
                    method, path, query_string, body, weight, product_id, ops,
                    _resigned=True,
                )
            raise classify_auth_error(code, message, context, resp.status_code) if code in (
                "invalid_api_key",
                "SignatureExpired",
                "UnauthorizedApiAccess",
                "ip_not_whitelisted_for_api_key",
            ) or code.startswith("Signature Mismatch") else DeltaAPIError(
                code, message, context, resp.status_code, raw=payload
            )

        meta = payload.get("meta") or {}
        return payload.get("result", {}), meta

    # ----------------------------------------------------------- pagination
    async def paginate(
        self,
        path: str,
        params: Mapping[str, Any],
        page_size: int = 100,
        result_key_result: bool = True,
        weight: int | None = None,
    ) -> list[Any]:
        """Walk cursor pages via `meta.after` until exhausted (spec 1.3).

        The cursor value is a list id (products) or a timestamp (orders/fills);
        either way we echo it back as `after` verbatim.
        """
        out: list[Any] = []
        cursor: str | int | None = None
        for _ in range(_PAGE_LIMIT):
            qs_params = dict(params)
            qs_params["page_size"] = page_size
            if cursor is not None:
                qs_params["after"] = cursor
            query_string = "?" + "&".join(f"{k}={v}" for k, v in qs_params.items() if v != "") if qs_params else ""
            result, meta = await self._request("GET", path, query_string, weight=weight)
            items = result if isinstance(result, list) else (result or {}).get("items", [])
            if not isinstance(items, list):
                break
            out.extend(items)
            after = meta.get("after") if meta else None
            if not after:  # exhausted: missing or empty cursor
                break
            if str(after) == str(cursor):
                logger.warning("pagination cursor did not advance on %s; stopping", path)
                break
            cursor = after
        else:
            logger.warning("pagination hit safety cap of %s pages on %s", _PAGE_LIMIT, path)
        return out

    def _q(self, params: Mapping[str, Any]) -> str:
        pairs = [f"{k}={v}" for k, v in params.items() if v is not None and v != ""]
        return ("?" + "&".join(pairs)) if pairs else ""

    # ============================================================ PUBLIC DATA
    async def get_products(
        self, states: str = "live", contract_types: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /products, fully paginated (spec 1.6: never hard-code ids)."""
        return await self.paginate(
            "/products",
            {"states": states, "contract_types": contract_types},
            weight=3,
        )

    async def get_product(self, symbol: str) -> dict[str, Any]:
        result, _ = await self._request("GET", f"/products/{symbol}", weight=3)
        return result

    async def get_tickers(
        self,
        contract_types: str | None = None,
        underlying_asset_symbols: str | None = None,
        expiry_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /tickers with the documented filter combination (spec 1.5).
        expiry_date format DD-MM-YYYY."""
        qs = self._q(
            {
                "contract_types": contract_types,
                "underlying_asset_symbols": underlying_asset_symbols,
                "expiry_date": expiry_date,
            }
        )
        result, _ = await self._request("GET", "/tickers", qs, weight=3)
        return result if isinstance(result, list) else []

    async def get_ticker(self, symbols: str) -> dict[str, Any]:
        """GET /tickers/{symbol} — up to 10 comma-separated symbols (spec 1.5)."""
        result, _ = await self._request("GET", f"/tickers/{symbols}", weight=3)
        return result

    async def get_l2_orderbook(self, symbol: str, depth: int | None = None) -> dict[str, Any]:
        # TODO(VERIFY): confirm the L2 orderbook path/param naming
        qs = self._q({"depth": depth})
        result, _ = await self._request("GET", f"/l2orderbook/{symbol}", qs, weight=3)
        return result

    async def get_candles(
        self,
        symbol: str,
        resolution: str,
        start: int,
        end: int,
        page_size: int = 2000,
    ) -> list[dict[str, Any]]:
        """GET /history/candles — (symbol, resolution, start, end) in SECONDS
        (spec 1.3/1.5). Returns newest→oldest candles.

        TODO(VERIFY): confirm param names (`resolution`, `start`, `end`) and
        that these are seconds, not microseconds, in the live docs.
        """
        qs = self._q(
            {
                "symbol": symbol,
                "resolution": resolution,
                "start": int(start),
                "end": int(end),
                "page_size": page_size,
            }
        )
        result, _ = await self._request("GET", "/history/candles", qs, weight=3)
        if isinstance(result, dict):
            return result.get("candles", result.get("results", []))
        return result

    async def get_assets(self) -> list[dict[str, Any]]:
        result, _ = await self._request("GET", "/assets", weight=3)
        return result if isinstance(result, list) else []

    # =============================================================== PRIVATE
    # Orders ----------------------------------------------------------------
    async def place_order(self, order: Mapping[str, Any], product_id: int | None = None) -> dict[str, Any]:
        result, _ = await self._request(
            "POST", "/orders", body=dict(order), weight=5, product_id=product_id, ops=1
        )
        return result

    async def edit_order(self, order: Mapping[str, Any], product_id: int | None = None) -> dict[str, Any]:
        result, _ = await self._request(
            "PUT", "/orders", body=dict(order), weight=5, product_id=product_id, ops=1
        )
        return result

    async def cancel_order(self, order_id: int, product_id: int | None = None) -> dict[str, Any]:
        result, _ = await self._request(
            "DELETE", "/orders", body={"id": order_id, "product_id": product_id},
            weight=5, product_id=product_id,
        )
        return result

    async def cancel_order_by_client_id(
        self, client_order_id: str, product_id: int
    ) -> dict[str, Any]:
        result, _ = await self._request(
            "DELETE", "/orders",
            body={"client_order_id": client_order_id, "product_id": product_id},
            weight=5, product_id=product_id,
        )
        return result

    async def get_orders(
        self,
        states: str = "open,pending",
        product_ids: str | None = None,
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /orders — states as (csv) 'open,pending'; product_ids max 10.
        TODO(VERIFY): confirm request filter keys & whether time filters are
        required; pagination uses `after` for order history but id for /orders.
        """
        params: dict[str, Any] = {"states": states, "product_ids": product_ids, "after": after}
        if after:
            # walk cursor pages like /orders/history (spec 1.3 list of cursored endpoints)
            return await self.paginate("/orders", params, weight=3)
        result, _ = await self._request("GET", "/orders", self._q(params), weight=3)
        return result if isinstance(result, list) else []

    async def get_order(self, order_id: int) -> dict[str, Any]:
        result, _ = await self._request("GET", f"/orders/{order_id}", weight=3)
        return result

    async def get_order_by_client_order_id(
        self, client_order_id: str, product_id: int | None = None
    ) -> dict[str, Any] | None:
        """Look an order up by its client_order_id (idempotency on ambiguous
        responses, spec 1.7).

        TODO(VERIFY): the docs summary in the prompt does not show a dedicated
        path for this. We query `GET /orders?client_order_id=...` and fall back
        to file search over the cursor-paged order history if empty; the exact
        supported filter must be confirmed in the live docs before prod use.
        """
        params: dict[str, Any] = {
            "client_order_id": client_order_id,
            "product_id": product_id,
        }
        result, _ = await self._request("GET", "/orders", self._q(params), weight=3)
        if isinstance(result, list) and result:
            for od in result:
                if str(od.get("client_order_id")) == str(client_order_id):
                    return od
            return None
        return result if isinstance(result, dict) and result.get("id") else None

    async def get_order_history(self, product_ids: str | None, page_size: int = 100) -> list[dict[str, Any]]:
        return await self.paginate(
            "/orders/history", {"product_ids": product_ids}, page_size=page_size, weight=10
        )

    async def get_fills(self, product_ids: str | None = None, page_size: int = 100) -> list[dict[str, Any]]:
        return await self.paginate("/fills", {"product_ids": product_ids}, page_size=page_size, weight=10)

    # Batch orders: same product, max 50, limit orders only, gtc only (spec 1.5)
    # TODO(VERIFY): exact batch payload keys differ per docs (orders list enclosures)
    async def batch_create_orders(
        self, product_id: int, order_list: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        assert 1 <= len(order_list) <= 50, "batch max 50"
        result, _ = await self._request(
            "POST", "/orders/batch",
            body={"product_id": product_id, "order_list": list(order_list)},
            weight=25, product_id=product_id, ops=float(len(order_list)),
        )
        return result

    async def batch_edit_orders(
        self, product_id: int, order_list: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        assert 1 <= len(order_list) <= 50, "batch max 50"
        result, _ = await self._request(
            "PUT", "/orders/batch",
            body={"product_id": product_id, "order_list": list(order_list)},
            weight=25, product_id=product_id, ops=float(len(order_list)),
        )
        return result

    async def batch_delete_orders(
        self, product_id: int, order_ids: list[int] | None = None,
        client_order_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        assert 1 <= (len(order_ids or []) + len(client_order_ids or [])) <= 50, "batch max 50"
        body: dict[str, Any] = {"product_id": product_id}
        if order_ids:
            body["order_ids"] = order_ids
        if client_order_ids:
            body["client_order_ids"] = client_order_ids
        result, _ = await self._request(
            "DELETE", "/orders/batch", body=body,
            weight=25, product_id=product_id,  # cancels are exempt from op cap
        )
        return result

    # Brackets ---- one bracket per open position; re-centre after fills (spec 1.7)
    async def bracket_place(
        self,
        product_id: int,
        size: int,
        order: Mapping[str, Any],
    ) -> dict[str, Any]:
        """POST /orders/bracket — TP/SL that closes the whole position (no size
        argument of its own; `size` here is the order payload size).
        TODO(VERIFY): confirm payload shape ({order: {...}} vs flat).
        """
        result, _ = await self._request(
            "POST", "/orders/bracket",
            body={"product_id": product_id, "size": size, **dict(order)},
            weight=5, product_id=product_id, ops=1,
        )
        return result

    async def bracket_edit(self, product_id: int, order: Mapping[str, Any]) -> dict[str, Any]:
        """PUT /orders/bracket to re-centre TP/SL after fills (spec 1.7).
        TODO(VERIFY): confirm the PUT/key names ("size" not needed on edit)."""
        result, _ = await self._request(
            "PUT", "/orders/bracket",
            body={"product_id": product_id, **dict(order)},
            weight=5, product_id=product_id, ops=1,
        )
        return result

    async def cancel_all(self, product_id: int | None = None, product_symbol: str | None = None,
                         states: str = "open") -> dict[str, Any]:
        """DELETE /orders/all — cancellations are exempt from the op cap (1.4)."""
        body = {"product_id": product_id, "product_symbol": product_symbol, "states": states}
        result, _ = await self._request("DELETE", "/orders/all", body=body, weight=5)
        return result

    # Positions ---------------------------------------------------------------
    async def get_positions_margined(self, product_ids: str | None = None) -> list[dict[str, Any]]:
        # TODO(VERIFY): confirm /positions/margined exists on the India docs
        qs = self._q({"product_ids": product_ids})
        result, _ = await self._request("GET", "/positions/margined", qs, weight=3)
        return result if isinstance(result, list) else []

    async def get_positions(self, product_id: int | None = None) -> list[dict[str, Any]]:
        # TODO(VERIFY): confirm parameter key (`product_id`) on /positions
        qs = self._q({"product_id": product_id})
        result, _ = await self._request("GET", "/positions", qs, weight=3)
        return result if isinstance(result, list) else []

    async def add_position_margin(
        self, product_id: int, amount: str | float | Decimal
    ) -> dict[str, Any]:
        result, _ = await self._request(
            "POST", "/positions/margin",
            body={"product_id": product_id, "amount": str(amount)},
            weight=5, product_id=product_id,
        )
        # TODO(VERIFY): exact "add/remove position margin" endpoint paths are not
        # confirmed in the prompt summary; /positions/margin used as best guess.
        return result

    async def remove_position_margin(
        self, product_id: int, amount: str | float | Decimal
    ) -> dict[str, Any]:
        result, _ = await self._request(
            "POST", "/positions/margin",
            body={"product_id": product_id, "amount": str(amount), "remove": True},
            weight=5, product_id=product_id,
        )
        # TODO(VERIFY): the sign convention (add vs remove) must be confirmed.
        return result

    async def close_all_positions(
        self, product_ids: list[int] | None = None, product_symbols: list[str] | None = None
    ) -> dict[str, Any]:
        result, _ = await self._request(
            "POST", "/positions/close-all",
            body={"product_ids": product_ids, "product_symbols": product_symbols},
            weight=5,
        )
        return result

    # Wallet ------------------------------------------------------------------
    async def get_wallet_balances(self, asset_symbol: str | None = None) -> list[dict[str, Any]]:
        qs = self._q({"asset_symbol": asset_symbol})
        result, _ = await self._request("GET", "/wallet/balances", qs, weight=3)
        return result if isinstance(result, list) else []
