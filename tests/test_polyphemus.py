from __future__ import annotations

import pytest

from tests.test_gateway_fallback_billing import _client_and_key
from trusted_router.catalog import MODELS, endpoints_for_model, model_to_openrouter_shape
from trusted_router.polyphemus import MODEL_ID, SELECT_ROUTE_TYPE
from trusted_router.storage import STORE


def test_polyphemus_catalog_is_responses_only_standard_privacy() -> None:
    model = MODELS[MODEL_ID]
    shape = model_to_openrouter_shape(model)
    assert shape["pricing"]["request"] == "0.000001"
    tr = shape["trustedrouter"]
    assert tr["supports_chat"] is False
    assert tr["supports_responses"] is True
    assert tr["pricing_type"] == "selection_fee_plus_selected_model_tokens"
    assert tr["provider_zero_data_retention"] is False
    assert tr["provider_e2ee"] is False
    assert tr["privacy_tier"] == 0
    assert tr["byok_available"] is False
    endpoints = endpoints_for_model(MODEL_ID)
    assert len(endpoints) == 1
    assert endpoints[0].provider == "telluvian"
    assert endpoints[0].request_price_microdollars == 1


def _admission(key: dict, **extra: object) -> dict:
    return {
        "api_key_hash": key["hash"],
        "model": MODEL_ID,
        "route_type": SELECT_ROUTE_TYPE,
        "estimated_input_tokens": 1000,
        "max_output_tokens": 1,
        "idempotency_key": "polyphemus-selection",
        **extra,
    }


def test_polyphemus_reserves_and_settles_exactly_one_microdollar() -> None:
    client, key = _client_and_key()
    money = STORE.credit_money[key["workspace_id"]]
    before = money.total_usage_microdollars
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key))
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["estimated_cost_microdollars"] == 1
    assert auth["model"] == MODEL_ID
    assert auth["provider"] == "telluvian"
    replay = client.post("/v1/internal/gateway/authorize", json=_admission(key))
    assert replay.status_code == 409, replay.text
    settlement = {
        "authorization_id": auth["authorization_id"],
        "actual_input_tokens": 0,
        "actual_output_tokens": 0,
        "route_type": SELECT_ROUTE_TYPE,
        "request_id": "selector-result",
        "elapsed_seconds": 0.15,
    }
    for _ in range(2):
        result = client.post("/v1/internal/gateway/settle", json=settlement)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["cost_microdollars"] == 1
        assert money.total_usage_microdollars == before + 1


def test_polyphemus_failed_selection_refunds_hold() -> None:
    client, key = _client_and_key()
    money = STORE.credit_money[key["workspace_id"]]
    before = money.total_usage_microdollars
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key))
    assert response.status_code == 200, response.text
    result = client.post(
        "/v1/internal/gateway/refund",
        json={
            "authorization_id": response.json()["data"]["authorization_id"],
            "route_type": SELECT_ROUTE_TYPE,
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "elapsed_seconds": 0.1,
            "status_code": 502,
            "error_type": "model_selection_failed",
        },
    )
    assert result.status_code == 200, result.text
    assert money.total_usage_microdollars == before


@pytest.mark.parametrize("provider", [
    {"min_privacy": "zdr"}, {"min_privacy": "confidential"},
    {"zdr": True}, {"data_collection": "deny"}, {"usage": "byok"},
    {"jurisdiction": "eu"}, {"jurisdiction": "us"}, {"only": ["openai"]},
])
def test_polyphemus_rejects_incompatible_provider_constraints(provider: dict) -> None:
    client, key = _client_and_key()
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key, provider=provider))
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("route", ["responses", "chat.completions", "messages", "decide"])
def test_polyphemus_cannot_bypass_selector(route: str) -> None:
    client, key = _client_and_key()
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key, route_type=route))
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("model,models", [
    (MODEL_ID, ["openai/gpt-4o"]),
    ("openai/gpt-4o", [MODEL_ID]),
    ("openai/gpt-4o", [MODEL_ID + ":nitro"]),
])
def test_polyphemus_rejects_fallback_arrays(model: str, models: list[str]) -> None:
    client, key = _client_and_key()
    response = client.post(
        "/v1/internal/gateway/authorize",
        json=_admission(key, model=model, models=models),
    )
    assert response.status_code == 400, response.text
