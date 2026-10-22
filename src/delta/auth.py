"""Request signing for Delta Exchange India (spec 1.2).

`signature = hex( HMAC_SHA256(api_secret, method + timestamp + path + query_string + body) )`

- method upper-case
- timestamp = unix SECONDS as a string, identical to the header value
- `query_string` includes the leading '?' or is an empty string
- `body` = the EXACT raw JSON string that is sent (serialize once, sign once,
  send the same string; spec 1.2 / 1.3)
- A signature is valid for ~5 seconds after generation (STALE_AFTER_S);
  callers re-sign with a fresh timestamp at most once on `SignatureExpired`.

NOTE (spec 1.2): the sample signature printed in the docs does NOT reproduce
with the printed sample secret, so it is never used as a test vector — tests
recompute the HMAC independently (tests/test_auth.py).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Mapping

DEFAULT_USER_AGENT = "fund-manager/0.1.0 (+delta-exchange-india; python-httpx)"
STALE_AFTER_S = 5.0

# Error codes the spec asks us to handle explicitly (spec 1.2).
AUTH_ERROR_CODES = (
    "invalid_api_key",
    "SignatureExpired",
    "UnauthorizedApiAccess",
    "ip_not_whitelisted_for_api_key",
    "Signature Mismatch",  # docs print it with a space; matched loosely below
)


class DeltaAuthError(Exception):
    """Explicit auth failure with the offending code / context attached."""

    def __init__(
        self,
        code: str,
        message: str = "",
        context: Any = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}".strip())
        self.code = code
        self.message = message
        self.context = context
        self.http_status = http_status


def _norm_query(query_string: str) -> str:
    """Signature uses the query string WITH its leading '?' (spec 1.2)."""
    qs = (query_string or "").strip()
    if qs and not qs.startswith("?"):
        qs = "?" + qs
    return qs


def sign_request(
    method: str,
    path: str,
    query_string: str,
    body: str,
    timestamp_str: str,
    api_secret: str,
) -> str:
    """Hex HMAC-SHA256 signature per spec 1.2. Pure and unit-testable."""
    payload = (
        method.upper()
        + timestamp_str
        + path
        + _norm_query(query_string)
        + (body or "")
    )
    return hmac.new(
        api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class AuthSigner:
    """Builds the signed header set for every private request (spec 1.2).

    The timestamp is generated here and reused verbatim as the `timestamp`
    header so it always matches the signed string.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        clock: Any = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.user_agent = user_agent
        # clock is injectable for stale-signature tests (monkeypatched time).
        self._clock_fixed = clock
        self.last_timestamp: str = ""

    @property
    def _clock(self) -> Any:
        # resolved lazily so tests can monkeypatch delta.auth.time.time
        return self._clock_fixed if self._clock_fixed is not None else time.time

    def timestamp(self) -> str:
        return str(int(self._clock()))

    def is_stale(self, timestamp_value: int | float | str, now: float | None = None) -> bool:
        """True when the signed timestamp is older than the validity window."""
        ts = float(timestamp_value)
        current = self._clock() if now is None else float(now)
        return (current - ts) > STALE_AFTER_S

    def signed_headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
        timestamp_str: str | None = None,
    ) -> dict[str, str]:
        """Headers for a private request. Public requests use plain_headers()."""
        ts = timestamp_str if timestamp_str is not None else self.timestamp()
        self.last_timestamp = ts
        signature = sign_request(method, path, query_string, body, ts, self.api_secret)
        headers = self.plain_headers()
        headers.update(
            {
                "api-key": self.api_key,
                "signature": signature,
                "timestamp": ts,
            }
        )
        return headers

    def plain_headers(self) -> dict[str, str]:
        """Common headers on every request; User-Agent is mandatory (spec 1.2)."""
        return {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


def is_auth_error_payload(payload: Mapping[str, Any]) -> bool:
    """True when an error envelope carries one of the known auth failures."""
    err = payload.get("error") or {}
    code = str(err.get("code", ""))
    return any(code == c or (c == "Signature Mismatch" and "signature" in code.lower())
               for c in AUTH_ERROR_CODES)


def classify_auth_error(
    code: str, message: str = "", context: Any = None, http_status: int | None = None
) -> DeltaAuthError:
    """Map a raw error into the explicitly-handled auth failures (spec 1.2)."""
    if code == "SignatureExpired":
        msg = "signature older than the 5s validity window; re-sign once with a fresh timestamp"
    elif code == "ip_not_whitelisted_for_api_key":
        # The server reports the request IP in the context; surface it verbatim.
        msg = f"IP not whitelisted for this API key (add IPv4 and IPv6 entries); context={context!r} {message}"
    elif code == "Signature Mismatch":
        msg = "signature mismatch: body/query must be the exact raw strings that were signed"
    else:
        msg = message
    return DeltaAuthError(code, msg, context, http_status)
