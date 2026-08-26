"""Lazy API-key federation.

The tests that matter here are the failure modes, not the happy path. A
standalone plane exists so a home-plane outage does not take it down; if
federation gets that wrong it converts an outage in one cloud into an
outage in both, which is worse than not federating at all.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

from trusted_router.services.federation import (
    BREAKER_THRESHOLD,
    FederationClient,
    FederationUnavailable,
)
from trusted_router.storage_models import federated_api_key_from_record

RECORD = {
    "lookup_hash": "lh-abc",
    "key_hash": "kh-abc",
    "workspace_id": "ws-1",
    "name": "prod key",
    "scopes": ["inference", "profile"],
    "disabled": False,
    "limit_microdollars": 5_000,
    "limit_daily_microdollars": 100,
    "include_byok_in_limit": True,
    "revision": "2026-08-02T00:00:00Z",
}


def _client(handler: Any) -> FederationClient:
    transport = httpx.MockTransport(handler)
    return FederationClient(
        home_base_url="https://trustedrouter.com",
        peer_token="peer-secret",  # noqa: S106 - test fixture.
        client=httpx.Client(transport=transport),
    )


class TestResolve:
    def test_resolves_and_returns_record(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-trustedrouter-federation-token"] == "peer-secret"
            return httpx.Response(200, json={"data": RECORD})

        assert _client(handler).resolve("lh-abc") == RECORD

    def test_404_means_no_such_key(self) -> None:
        client = _client(lambda _r: httpx.Response(404, json={"error": {}}))
        assert client.resolve("lh-missing") is None

    def test_negative_result_is_cached(self) -> None:
        """A leaked or rotated key in a retry loop must not become
        sustained cross-border QPS."""
        calls = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404, json={})

        client = _client(handler)
        assert client.resolve("lh-gone") is None
        assert client.resolve("lh-gone") is None
        assert calls["n"] == 1, "second lookup should hit the negative cache"


class TestUnavailability:
    def test_connect_error_raises_unavailable_not_none(self) -> None:
        """None would become 401 — telling a paying customer their key is
        bad because OUR upstream is down."""
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("home unreachable")

        with pytest.raises(FederationUnavailable):
            _client(handler).resolve("lh-abc")

    def test_5xx_is_unavailable_not_unknown_key(self) -> None:
        client = _client(lambda _r: httpx.Response(503, json={}))
        with pytest.raises(FederationUnavailable):
            client.resolve("lh-abc")

    def test_bad_peer_token_is_unavailable_and_not_negative_cached(self) -> None:
        """401 here means OUR token is misconfigured — a fact about this
        plane, not a verdict about the key. Caching it as 'unknown' would
        turn a config error into silent per-key 401s that outlive the fix."""
        calls = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={})

        client = _client(handler)
        with pytest.raises(FederationUnavailable):
            client.resolve("lh-abc")
        with pytest.raises(FederationUnavailable):
            client.resolve("lh-abc")
        assert calls["n"] == 2, "auth failure must not be negative-cached"

    def test_breaker_opens_after_repeated_failures(self) -> None:
        """Without a breaker, a home-plane outage exhausts this plane's
        worker pool waiting on timeouts — a US outage would take EU down,
        the exact coupling federation exists to avoid."""
        calls = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("down")

        client = _client(handler)
        for _ in range(BREAKER_THRESHOLD):
            with pytest.raises(FederationUnavailable):
                client.resolve(f"lh-{_}")
        before = calls["n"]

        with pytest.raises(FederationUnavailable, match="circuit breaker"):
            client.resolve("lh-after-open")
        assert calls["n"] == before, "an open breaker must not make a network call"


class TestSingleFlight:
    def test_concurrent_cold_lookups_make_one_call(self) -> None:
        """A popular key arriving on a cold plane must not fan out into a
        thundering herd against the plane we are trying not to depend on."""
        calls = {"n": 0}
        gate = threading.Event()

        def handler(_r: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            gate.wait(timeout=2)
            return httpx.Response(200, json={"data": RECORD})

        client = _client(handler)
        threads = [threading.Thread(target=lambda: client.resolve("lh-hot")) for _ in range(4)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(timeout=5)

        assert calls["n"] <= 2, f"expected single-flight, got {calls['n']} calls"


class TestFederatedRecordSafety:
    def test_no_secret_material_is_carried(self) -> None:
        """The peer must be unable to authenticate a raw bearer token for a
        federated key: it holds no secret_hash, so verify fails closed and
        only the attested-gateway lookup-hash path can resolve it."""
        key = federated_api_key_from_record(RECORD)
        assert key.salt == ""
        assert key.secret_hash == ""

    def test_credits_and_usage_start_at_zero(self) -> None:
        """Identity federates; money does not. Copying a balance mints it."""
        key = federated_api_key_from_record(RECORD)
        assert key.usage_microdollars == 0
        assert key.byok_usage_microdollars == 0
        assert key.reserved_microdollars == 0

    def test_limits_are_carried(self) -> None:
        key = federated_api_key_from_record(RECORD)
        assert key.limit_microdollars == 5_000
        assert key.limit_daily_microdollars == 100
        assert key.include_byok_in_limit is True

    def test_scopes_are_carried_and_missing_scopes_are_legacy(self) -> None:
        assert federated_api_key_from_record(RECORD).scopes == ["inference", "profile"]
        assert federated_api_key_from_record({k: v for k, v in RECORD.items() if k != "scopes"}).scopes == []

    def test_never_management(self) -> None:
        """A management key can mint keys and move money. Even if a home
        plane somehow served one, the peer must not honour it."""
        key = federated_api_key_from_record({**RECORD, "management": True})
        assert key.management is False

    def test_marked_as_federated(self) -> None:
        assert federated_api_key_from_record(RECORD).federated_home != ""

    def test_disabled_flag_is_carried(self) -> None:
        assert federated_api_key_from_record({**RECORD, "disabled": True}).disabled is True
