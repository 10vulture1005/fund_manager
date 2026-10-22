"""Signer unit tests — spec 1.2 / 3.7.

The docs' printed sample signature does NOT reproduce with the sample secret,
so we never use it as a vector. Instead every expected signature is computed
in-line here with an INDEPENDENT hmac/hashlib implementation and compared
against `delta.auth.sign_request`, plus covariance checks (change one input →
different signature) and the 5-second stale/timestamp handling via monkeypatched
time.
"""
from __future__ import annotations

import hashlib
import hmac

from delta.auth import (
    DEFAULT_USER_AGENT,
    AuthSigner,
    STALE_AFTER_S,
    is_auth_error_payload,
    sign_request,
)

SECRET = "test-secret-0123456789abcdef"


def independent_hmac(payload: str, api_secret: str) -> str:
    # Reimplemented here on purpose (spec 3.7): derived from the formula
    # in the docs, not from delta.auth internals.
    return hmac.new(
        api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class TestSignAgainstIndependentHmac:
    def test_vector_1_get_no_body(self):
        ts = "1710000000"
        expected = independent_hmac("GET" + ts + "/v2/orders" + "", SECRET)
        assert sign_request("GET", "/v2/orders", "", "", ts, SECRET) == expected

    def test_vector_2_get_with_query(self):
        ts = "1710000001"
        qs = "?product_id=1&state=open"
        expected = independent_hmac("GET" + ts + "/v2/orders" + qs, SECRET)
        assert sign_request("GET", "/v2/orders", qs, "", ts, SECRET) == expected

    def test_vector_3_post_body(self):
        ts = "1710000002"
        body = '{"order":{"size":11,"side":"buy"}}'
        expected = independent_hmac("POST" + ts + "/v2/orders" + body, SECRET)
        assert sign_request("POST", "/v2/orders", "", body, ts, SECRET) == expected

    def test_method_uppercased(self):
        ts = "1710000003"
        assert sign_request("get", "/v2/x", "", "", ts, SECRET) == (
            sign_request("GET", "/v2/x", "", "", ts, SECRET)
        )

    def test_query_string_leading_q_added_if_missing(self):
        ts = "1710000004"
        missing = sign_request("GET", "/v2/x", "a=1", "", ts, SECRET)
        present = sign_request("GET", "/v2/x", "?a=1", "", ts, SECRET)
        assert missing == present  # leading '?' is normalised in


class TestCovariance:
    """Changing any signed element must change the signature."""

    BASE = dict(method="POST", path="/v2/orders", query_string="",
                body='{"a":1}', timestamp_str="1700000005")

    def _sig(self, **overrides):
        kwargs = {**self.BASE, **overrides}
        return sign_request(
            kwargs["method"], kwargs["path"], kwargs["query_string"],
            kwargs["body"], kwargs["timestamp_str"], SECRET,
        )

    def test_timestamp_changes_signature(self):
        assert self._sig() != self._sig(timestamp_str="1700000006")

    def test_path_changes_signature(self):
        assert self._sig() != self._sig(path="/v2/orders/batch")

    def test_query_changes_signature(self):
        assert self._sig() != self._sig(query_string="?product_id=8")

    def test_body_changes_signature(self):
        assert self._sig() != self._sig(body='{"a":2}')

    def test_secret_changes_signature(self):
        sa = sign_request(
            self.BASE["method"], self.BASE["path"], self.BASE["query_string"],
            self.BASE["body"], self.BASE["timestamp_str"], SECRET,
        )
        sb = sign_request(
            self.BASE["method"], self.BASE["path"], self.BASE["query_string"],
            self.BASE["body"], self.BASE["timestamp_str"], SECRET + "x",
        )
        assert sa != sb


class TestAuthSignerTiming:
    """Signature validity window (spec 1.2: sign fresh per request, re-sign
    once on SignatureExpired) — clock monkeypatched here."""

    def test_fresh_signature_not_stale(self, monkeypatch):
        now = [1_700_000_000.0]
        monkeypatch.setattr("delta.auth.time.time", lambda: now[0])
        signer = AuthSigner(api_key="k", api_secret=SECRET)
        ts = signer.timestamp()
        assert not signer.is_stale(ts)

    def test_signature_goes_stale_after_5s(self, monkeypatch):
        now = [1_700_000_000.0]
        monkeypatch.setattr("delta.auth.time.time", lambda: now[0])
        signer = AuthSigner(api_key="k", api_secret=SECRET)
        ts = signer.timestamp()
        now[0] += STALE_AFTER_S + 0.5  # 6s later
        assert signer.is_stale(ts, now=None)
        # still valid at +4s (inside the window)
        now[0] = 1_700_000_000.0 + 4.0
        assert not signer.is_stale(ts, now=None)

    def test_headers_carry_signed_timestamp_and_ua(self, monkeypatch):
        now = [1_700_000_100.0]
        monkeypatch.setattr("delta.auth.time.time", lambda: now[0])
        signer = AuthSigner(api_key="k", api_secret=SECRET)
        headers = signer.signed_headers("POST", "/v2/orders", "", '{"a":1}')
        expected = independent_hmac(
            "POST" + headers["timestamp"] + "/v2/orders" + '{"a":1}', SECRET
        )
        assert headers["signature"] == expected
        assert headers["timestamp"] == "1700000100"
        assert headers["api-key"] == "k"
        assert headers["User-Agent"] == DEFAULT_USER_AGENT
        assert headers["Content-Type"] == "application/json"


def test_auth_error_payload_recognised():
    payload = {"success": False, "error": {"code": "SignatureExpired", "error": {}}}
    assert is_auth_error_payload(payload)
    assert not is_auth_error_payload({"success": False, "error": {"code": "order_not_found"}})
