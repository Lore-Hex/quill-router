"""Gateway authorize reads its clock once.

Authorize prices the hold at one instant, and settlement prices a request
without a pricing snapshot at the authorization's `created_at`. The store
used to stamp `created_at` from a second, later reading, so a scheduled price
change that fell between the two held the request at one tariff and billed
it at the other. The instant that priced the hold is now the one stored.
"""

from __future__ import annotations

import datetime as real_dt
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.fixture_routes import serve_on_fixture_route
from tests.test_gateway_authorize_spanner_operations import (
    _body,
    _request,
    _seed_typed_gateway_store,
)
from trusted_router.catalog import effective_endpoint
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_lifecycle import PHALA_JULY_2026_EFFECTIVE_AT
from trusted_router.routes.internal import gateway
from trusted_router.storage import STORE

# A model whose provider price steps up once, for good, at a fixed instant.
_MODEL = "qwen/qwen-2.5-7b-instruct"
_HOST = "phala"
_BEFORE = PHALA_JULY_2026_EFFECTIVE_AT - real_dt.timedelta(seconds=1)


def _iso(instant: real_dt.datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def _authorize_at(monkeypatch: pytest.MonkeyPatch, instant: real_dt.datetime) -> None:
    """Gateway authorize reads `instant` from its clock. The store's own
    clock, and every other module's, still reads the real time."""

    class _Clock(real_dt.datetime):
        @classmethod
        def now(cls, tz: real_dt.tzinfo | None = None) -> real_dt.datetime:  # type: ignore[override]
            return instant if tz is None else instant.astimezone(tz)

    clock = SimpleNamespace(**{name: getattr(real_dt, name) for name in dir(real_dt) if not name.startswith("__")})
    clock.datetime = _Clock
    monkeypatch.setattr(gateway, "dt", clock)


def test_the_typed_authorization_is_stamped_with_the_instant_that_priced_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed_typed_gateway_store()
    _authorize_at(monkeypatch, _BEFORE)

    response = gateway._authorize_gateway_sync(_request(), _body(key.hash), Settings(environment="test"))

    authorization = store.get_gateway_authorization(response["data"]["authorization_id"])
    assert authorization is not None
    assert authorization.created_at == _iso(_BEFORE)


def test_the_legacy_authorization_is_stamped_with_the_instant_that_priced_it(
    monkeypatch: pytest.MonkeyPatch, client: TestClient,
) -> None:
    serve_on_fixture_route(monkeypatch, _MODEL, _HOST, author="qwen")
    key = _new_key(client)
    _authorize_at(monkeypatch, _BEFORE)

    authorization_id = _authorize(client, key)

    authorization = STORE.get_gateway_authorization(authorization_id)
    assert authorization is not None
    assert authorization.created_at == _iso(_BEFORE)


def test_a_price_change_between_two_clock_readings_does_not_split_the_bill(
    monkeypatch: pytest.MonkeyPatch, client: TestClient,
) -> None:
    route = serve_on_fixture_route(monkeypatch, _MODEL, _HOST, author="qwen")
    before = effective_endpoint(route, at=_BEFORE).prompt_price_microdollars_per_million_tokens
    after = effective_endpoint(route, at=PHALA_JULY_2026_EFFECTIVE_AT).prompt_price_microdollars_per_million_tokens
    # Control: the price the hold was estimated at is not the price today.
    assert before != after
    key = _new_key(client)
    _authorize_at(monkeypatch, _BEFORE)
    authorization_id = _authorize(client, key)

    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": 1_000_000,
            "actual_output_tokens": 0,
            "request_id": "one-clock-reading",
            "elapsed_seconds": 1,
        },
    )

    assert settled.status_code == 200, settled.text
    # A million prompt tokens cost the per-million prompt price at the
    # instant the hold was priced, not at the store's later reading.
    assert settled.json()["data"]["cost_microdollars"] == before


def _new_key(client: TestClient) -> dict:
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "one-clock@example.com"},
        json={"name": "one clock reading"},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]


def _authorize(client: TestClient, key: dict) -> str:
    authorized = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": _MODEL,
            "estimated_input_tokens": 1_000_000,
            "max_output_tokens": 1,
            "provider": {"only": [_HOST]},
        },
    )
    assert authorized.status_code == 200, authorized.text
    return str(authorized.json()["data"]["authorization_id"])


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(environment="test"), init_observability=False))
