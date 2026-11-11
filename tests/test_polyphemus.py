from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.test_gateway_fallback_billing import _client_and_key
from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    endpoints_for_model,
    model_to_openrouter_shape,
)
from trusted_router.catalog_data import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_TRUSTED,
    PRIVACY_TIER_ZERO_RETENTION,
    TRUSTED_PROVIDER_SLUGS,
)
from trusted_router.polyphemus import (
    LEGACY_SELECTOR_ENDPOINT_ID,
    MODEL_ID,
    SELECT_ROUTE_TYPE,
    SELECTABLE_MODEL_IDS,
    SELECTOR_ENDPOINT_ID,
)
from trusted_router.storage import STORE


def test_polyphemus_catalog_is_responses_only_with_local_trusted_selector() -> None:
    model = MODELS[MODEL_ID]
    shape = model_to_openrouter_shape(model)
    assert "request" not in shape["pricing"]
    assert float(shape["pricing"]["prompt"]) > 0
    assert float(shape["pricing"]["completion"]) > 0
    assert float(shape["pricing"]["completion_max"]) >= float(shape["pricing"]["completion"])
    tr = shape["trustedrouter"]
    assert tr["supports_chat"] is False
    assert tr["supports_responses"] is True
    assert tr["pricing_type"] == "selection_fee_plus_selected_model_tokens"
    assert tr["selector_pricing_unit"] == "prompt_tokens"
    assert tr["selector_prompt_price_per_million"] == "0.05"
    assert tr["selector_usage_estimated"] is True
    assert tr["selector_token_basis"] == "latest_user_message_utf8_bytes_div_4"  # noqa: S105 - meter label
    assert tr["byok_available"] is False
    assert model.provider == "trustedrouter"
    endpoints = endpoints_for_model(MODEL_ID)
    assert len(endpoints) == 1
    assert endpoints[0].provider == "trustedrouter"
    assert endpoints[0].id == SELECTOR_ENDPOINT_ID
    # The selection step runs in TrustedRouter's attested gateway: no third
    # party, nothing stored. Its own route is Trusted tier.
    [endpoint_row] = tr["endpoints"]
    assert endpoint_row["provider"] == "trustedrouter"
    assert endpoint_row["stores_content"] is False
    assert endpoint_row["provider_zero_data_retention"] is True
    assert endpoint_row["provider_confidential_compute"] is True
    assert endpoint_row["provider_e2ee"] is True
    assert endpoint_row["privacy_tier"] == PRIVACY_TIER_TRUSTED
    assert "No third party sees the prompt" in endpoint_row["provider_policy"]
    assert endpoints[0].request_price_microdollars == 0
    assert endpoints[0].prompt_price_microdollars_per_million_tokens == 50_000
    assert endpoints[0].completion_price_microdollars_per_million_tokens == 0


def test_polyphemus_selector_trusted_tier_is_scoped_to_the_selector_route() -> None:
    from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement

    [selector] = endpoints_for_model(MODEL_ID)
    for requirement in (
        PRIVACY_TIER_NO_STORE, PRIVACY_TIER_ZERO_RETENTION,
        PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_TRUSTED,
    ):
        assert endpoint_meets_privacy_requirement(selector, requirement)
    # TrustedRouter is not added to the reviewed upstream-operator allowlist,
    # and the exemption does not follow the provider slug to other models.
    assert "trustedrouter" not in TRUSTED_PROVIDER_SLUGS
    other = replace(selector, id="trustedrouter/auto@trustedrouter/prepaid", model_id="trustedrouter/auto")
    assert endpoint_meets_privacy_requirement(other, PRIVACY_TIER_CONFIDENTIAL)
    assert not endpoint_meets_privacy_requirement(other, PRIVACY_TIER_TRUSTED)


def test_polyphemus_price_envelope_covers_exactly_the_selectable_models() -> None:
    from trusted_router.catalog import _meta_price_range

    assert SELECTABLE_MODEL_IDS == (
        "anthropic/claude-opus-5.5",
        "deepseek/deepseek-v4.1-flash-1017",
        "moonshotai/kimi-k3.1-pro",
    )
    for attr in (
        "prompt_price_microdollars_per_million_tokens",
        "completion_price_microdollars_per_million_tokens",
        "published_prompt_price_microdollars_per_million_tokens",
        "published_completion_price_microdollars_per_million_tokens",
    ):
        values = [
            getattr(endpoint, attr)
            for model_id in SELECTABLE_MODEL_IDS
            for endpoint in endpoints_for_model(model_id)
            if endpoint.usage_type == "Credits" and getattr(endpoint, attr) > 0
        ]
        assert values, attr
        assert _meta_price_range(MODEL_ID, attr) == (min(values), max(values))


def test_polyphemus_standard_price_preview_includes_selector_fee() -> None:
    from trusted_router.catalog import _meta_price_range
    from trusted_router.money import microdollars_per_million_tokens_to_token_decimal

    shape = model_to_openrouter_shape(MODELS[MODEL_ID])
    low, high = _meta_price_range(MODEL_ID, "prompt_price_microdollars_per_million_tokens")
    assert shape["pricing"]["prompt"] == microdollars_per_million_tokens_to_token_decimal(low + 50_000)
    assert shape["pricing"]["prompt_max"] == microdollars_per_million_tokens_to_token_decimal(high + 50_000)


def test_polyphemus_catalog_documents_local_selection() -> None:
    shape = model_to_openrouter_shape(MODELS[MODEL_ID])
    documentation = shape["trustedrouter"]["documentation"]
    example = json.loads(documentation["example_input"])
    assert example["model"] == MODEL_ID
    assert "session_id" not in example
    description = documentation["description"]
    assert "telluvian" not in json.dumps(documentation).lower()
    assert "No third party sees your prompt" in description
    assert "inside TrustedRouter's attested gateway" in description
    assert "open classifier" in description
    for model_id in SELECTABLE_MODEL_IDS:
        assert model_id in description
    assert "Confidential, ZDR and no-store requests are supported" in description
    assert "classified latest user message only" in description
    assert "$0.05 per million selector prompt tokens" in description
    assert "without a selector fee" in description
    guidance = documentation["input_format"]
    assert "x_perf and polyphemus.model_zoo options are accepted but ignored" in guidance
    assert "session_id does not affect selection" in guidance


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


@pytest.mark.parametrize("tokens,cost", [(1, 1), (20, 1), (29, 1), (30, 2), (1000, 50), (60_000, 3000)])
def test_polyphemus_reserves_and_settles_selector_prompt_tokens_once(tokens: int, cost: int) -> None:
    client, key = _client_and_key()
    money = STORE.credit_money[key["workspace_id"]]
    before = money.total_usage_microdollars
    reserved_before = money.reserved_microdollars
    admission = _admission(key, estimated_input_tokens=tokens)
    response = client.post("/v1/internal/gateway/authorize", json=admission)
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["estimated_cost_microdollars"] == cost
    assert money.reserved_microdollars == reserved_before + cost
    assert auth["model"] == MODEL_ID
    assert auth["provider"] == "trustedrouter"
    replay = client.post("/v1/internal/gateway/authorize", json=admission)
    assert replay.status_code == 409, replay.text
    settlement = {
        "authorization_id": auth["authorization_id"],
        "actual_input_tokens": tokens,
        "actual_output_tokens": 0,
        "usage_estimated": True,
        "route_type": SELECT_ROUTE_TYPE,
        "request_id": "selector-result",
        "elapsed_seconds": 0.15,
    }
    for _ in range(2):
        result = client.post("/v1/internal/gateway/settle", json=settlement)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["cost_microdollars"] == cost
        assert money.total_usage_microdollars == before + cost
        assert money.reserved_microdollars == reserved_before
        generation = STORE.get_generation(result.json()["data"]["generation_id"])
        assert generation is not None
        assert generation.tokens_prompt == tokens
        assert generation.tokens_completion == 0
        assert generation.usage_estimated is True
        assert generation.total_cost_microdollars == cost


@pytest.mark.parametrize("old_admission", [True, False])
@pytest.mark.parametrize("actual_tokens", [0, 1000])
def test_polyphemus_settlement_survives_mixed_rollout(
    monkeypatch: pytest.MonkeyPatch, old_admission: bool, actual_tokens: int,
) -> None:
    client, key = _client_and_key()
    endpoint = endpoints_for_model(MODEL_ID)[0]
    with monkeypatch.context() as before_rollout:
        if old_admission:
            before_rollout.setitem(MODEL_ENDPOINTS, endpoint.id, replace(
                endpoint, prompt_price_microdollars_per_million_tokens=0,
                request_price_microdollars=1,
            ))
        response = client.post("/v1/internal/gateway/authorize", json=_admission(key))
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["estimated_cost_microdollars"] == (1 if old_admission else 50)
    money = STORE.credit_money[key["workspace_id"]]
    before = money.total_usage_microdollars
    for _ in range(2):
        result = client.post("/v1/internal/gateway/settle", json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": actual_tokens, "actual_output_tokens": 0,
            "usage_estimated": actual_tokens > 0,
            "route_type": SELECT_ROUTE_TYPE, "request_id": "legacy-selector-result",
        })
        assert result.status_code == 200, result.text
        cost = 1 if old_admission or actual_tokens == 0 else 50
        assert result.json()["data"]["cost_microdollars"] == cost
        assert money.total_usage_microdollars == before + cost
        assert money.reserved_microdollars == 0


def test_polyphemus_million_token_rate_uses_shared_integer_billing() -> None:
    from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars

    endpoint = endpoints_for_model(MODEL_ID)[0]
    assert _endpoint_cost_microdollars(endpoint, 1_000_000, 0) == 50_000
    assert _endpoint_cost_microdollars(endpoint, 1_000_000, 1_000_000) == 50_000


def test_polyphemus_failed_selection_refunds_hold() -> None:
    client, key = _client_and_key()
    money = STORE.credit_money[key["workspace_id"]]
    before = money.total_usage_microdollars
    reserved_before = money.reserved_microdollars
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key))
    assert response.status_code == 200, response.text
    assert money.reserved_microdollars == reserved_before + 50
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
    assert money.reserved_microdollars == reserved_before


@pytest.mark.parametrize("provider", [
    {"min_privacy": "zdr"}, {"min_privacy": "confidential"}, {"min_privacy": "trusted"},
    {"zdr": True}, {"data_collection": "deny"},
    {"min_privacy": "no-store"}, {"min_privacy": "e2e"}, {"min_privacy": "e2ee"},
    {"min_privacy": "max"}, {"min_privacy": "maximum"},
])
def test_polyphemus_admits_private_selector_requests(provider: dict) -> None:
    """Selection runs in the attested gateway and stores nothing, so the
    caller's no-retention and confidential constraints admit the selector."""
    client, key = _client_and_key()
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key, provider=provider))
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["model"] == MODEL_ID
    assert auth["provider"] == "trustedrouter"
    assert auth["estimated_cost_microdollars"] == 50


@pytest.mark.parametrize("provider", [
    {"usage": "byok"}, {"jurisdiction": "eu"}, {"only": ["openai"]}, {"ignore": ["trustedrouter"]},
])
def test_polyphemus_rejects_incompatible_provider_constraints(provider: dict) -> None:
    client, key = _client_and_key()
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key, provider=provider))
    assert response.status_code == 400, response.text


def test_polyphemus_legacy_telluvian_selector_hold_settles_on_current_route() -> None:
    """A selector hold admitted while Telluvian was the provider still names
    its old route; it settles once at the unchanged selector tariff."""
    client, key = _client_and_key()
    response = client.post("/v1/internal/gateway/authorize", json=_admission(key))
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert LEGACY_SELECTOR_ENDPOINT_ID not in MODEL_ENDPOINTS
    authorizations = STORE.api_keys.gateway_authorizations
    stored = authorizations[auth["authorization_id"]]
    assert stored.endpoint_id == SELECTOR_ENDPOINT_ID
    authorizations[auth["authorization_id"]] = replace(
        stored, endpoint_id=LEGACY_SELECTOR_ENDPOINT_ID,
        candidate_endpoint_ids=[LEGACY_SELECTOR_ENDPOINT_ID], provider="telluvian",
    )
    money = STORE.credit_money[key["workspace_id"]]
    before = money.total_usage_microdollars
    for _ in range(2):
        result = client.post("/v1/internal/gateway/settle", json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 1000, "actual_output_tokens": 0, "usage_estimated": True,
            "route_type": SELECT_ROUTE_TYPE, "request_id": "legacy-provider-selector-result",
        })
        assert result.status_code == 200, result.text
        assert result.json()["data"]["cost_microdollars"] == 50
        assert money.total_usage_microdollars == before + 50
        assert money.reserved_microdollars == 0


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
