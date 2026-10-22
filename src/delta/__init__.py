"""Delta Exchange India adapter package.

Implements the shared exchange layer used by both strategies
(spec prompt A/B Part 1): REST client, request signing, client-side rate
limiting, product discovery, order management, websocket feed and the
heartbeat-based deadman switch.
"""
from __future__ import annotations

from .auth import (
    DEFAULT_USER_AGENT,
    AuthSigner,
    DeltaAuthError,
    STALE_AFTER_S,
    classify_auth_error,
    is_auth_error_payload,
    sign_request,
)
from .rest import DeltaAPIError, DeltaRestClient, encode_json_body
from .ratelimit import RateLimiter, weight_for
from .products import ProductCatalog, product_info_from_api
from .orders import DeltaOrderManager
from .ws import DeltaWSClient
from .heartbeat import DeadmanSwitch

__all__ = [
    "DEFAULT_USER_AGENT",
    "AuthSigner",
    "DeltaAuthError",
    "STALE_AFTER_S",
    "is_auth_error_payload",
    "classify_auth_error",
    "sign_request",
    "DeltaAPIError",
    "DeltaRestClient",
    "encode_json_body",
    "RateLimiter",
    "weight_for",
    "ProductCatalog",
    "product_info_from_api",
    "DeltaOrderManager",
    "DeltaWSClient",
    "DeadmanSwitch",
]
