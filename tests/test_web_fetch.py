from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, endpoints_for_model
from trusted_router.storage import STORE


@pytest.fixture
def fetch_key(client: TestClient, user_headers: dict[str, str]) -> str:
    return str(client.post("/v1/keys", headers=user_headers, json={"name": "fetch"}).json()["data"]["hash"])


def authorize(client: TestClient, key: str, **changes: Any) -> Any:
    return client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key, "model": "deepseek/fetch", "route_type": "tools.fetch",
        "estimated_input_tokens": 0, "max_output_tokens": 1,
        "idempotency_key": "fetch-one", "request_fingerprint": "a" * 64, **changes,
    })


def settle_body(auth: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "authorization_id": auth["authorization_id"], "request_id": "fetch-result",
        "actual_input_tokens": 0, "actual_output_tokens": 0, "elapsed_seconds": 1,
        "route_type": "tools.fetch", "finish_reason": "stop", **changes,
    }


def test_fetch_catalog(client: TestClient) -> None:
    rows = {row["id"]: row for row in client.get("/v1/models").json()["data"]}
    row = rows["deepseek/fetch"]
    assert row["pricing"] == {"request": "0.000792"}
    assert row["trustedrouter"]["fetch"]["billing_unit"] == "successful_request"
    assert not MODELS["deepseek/fetch"].supports_chat
    endpoints = endpoints_for_model("deepseek/fetch")
    assert endpoints and all(not e.is_byok for e in endpoints)
    from trusted_router.routes.catalog import _endpoint_pricing_payload
    for endpoint in endpoints:
        assert _endpoint_pricing_payload(endpoint) == row["pricing"]


@pytest.mark.parametrize("path", ["/models/deepseek/fetch", "/models/deepseek/fetch/pricing"])
def test_fetch_public_price(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert "$0.000792 / successful request" in response.text


def test_fetch_public_example(client: TestClient) -> None:
    response = client.get("/models/deepseek/fetch/api")
    assert response.status_code == 200, response.text
    assert "/tools/fetch" in response.text
    assert "Not ZDR or confidential" in response.text


def test_fetch_frozen_fee_exactly_once(client: TestClient, fetch_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    response = authorize(client, fetch_key)
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["estimated_cost_microdollars"] == 792
    assert auth["additional_cost_reservation_microdollars"] == 0
    endpoint = MODEL_ENDPOINTS[auth["endpoint_id"]]
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, replace(endpoint, request_price_microdollars=99999))
    from trusted_router.routes.internal import gateway
    monkeypatch.setattr(gateway, "FETCH_UPSTREAM_COST_MICRODOLLARS", 99999)
    replay = authorize(client, fetch_key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["idempotent_replay"]
    mismatch = authorize(client, fetch_key, request_fingerprint="b" * 64)
    assert mismatch.status_code == 409, mismatch.text
    for _ in range(2):
        settled = client.post("/v1/internal/gateway/settle", json=settle_body(auth))
        assert settled.status_code == 200, settled.text
        data = settled.json()["data"]
        assert data["cost_microdollars"] == 792
        assert data["input_tokens"] == data["output_tokens"] == 0
    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None and generation.route_type == "tools.fetch"
    assert generation.operator_cost_microdollars == 750


@pytest.mark.parametrize("changes", [
    {"models": ["deepseek/fetch"]}, {"service_tier": "priority"},
    {"estimated_input_tokens": 1}, {"max_output_tokens": 2},
    {"route_type": "chat.completions"}, {"route_type": "responses"},
    {"request_fingerprint": ""}, {"additional_cost_reservation_microdollars": 1},
    {"inference_receipt": True}, {"provider": {"usage": "byok"}},
    {"provider": {"only": ["openai"]}}, {"provider": {"ignore": ["deepseek"]}},
    {"provider": {"min_privacy": "zdr"}}, {"provider": {"min_privacy": "confidential"}},
    {"provider": {"zdr": True}},
    {"provider": {"max_price": {"prompt": 1}}},
])
def test_fetch_invalid_admission(client: TestClient, fetch_key: str, changes: dict[str, Any]) -> None:
    response = authorize(client, fetch_key, **changes)
    assert response.status_code in (400, 422), response.text


@pytest.mark.parametrize("changes", [
    {"route_type": None}, {"route_type": "chat.completions"},
    {"actual_input_tokens": 1}, {"actual_output_tokens": 1},
    {"cache_read_input_tokens": 1}, {"cache_creation_input_tokens": 1},
    {"additional_cost_microdollars": 1}, {"service_tier": "priority"},
])
def test_fetch_invalid_settlement(client: TestClient, fetch_key: str, changes: dict[str, Any]) -> None:
    response = authorize(client, fetch_key)
    assert response.status_code == 200, response.text
    response = client.post("/v1/internal/gateway/settle", json=settle_body(response.json()["data"], **changes))
    assert response.status_code == 400, response.text


def test_fetch_refund(client: TestClient, fetch_key: str) -> None:
    response = authorize(client, fetch_key)
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    body = settle_body(auth, route_type=None, error_status=502, error_type="fetch_provider_error")
    for _ in range(2):
        response = client.post("/v1/internal/gateway/refund", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["cost_microdollars"] == 0


def test_fetch_enforces_key_limit(client: TestClient, user_headers: dict[str, str]) -> None:
    key = client.post("/v1/keys", headers=user_headers, json={"name": "tiny", "limit": 0.000791}).json()["data"]["hash"]
    response = authorize(client, key)
    assert response.status_code == 402, response.text


def test_fetch_app_markup(client: TestClient, user_headers: dict[str, str]) -> None:
    from tests.test_app_markup_billing import _mint_app_key
    _, raw = _mint_app_key(client, user_headers, app_id="fetch-app", bps=1000)
    key = STORE.get_key_by_raw(raw)
    assert key is not None
    response = authorize(client, key.hash)
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    # App markups use the existing floor rule: 792 * 10% -> 79 microdollars.
    assert auth["estimated_cost_microdollars"] == 871
    response = client.post("/v1/internal/gateway/settle", json=settle_body(auth))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == 871
