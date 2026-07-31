from __future__ import annotations

from fastapi.testclient import TestClient

from scripts.pricing.parsers.openai import parse as parse_openai_pricing
from trusted_router.catalog import endpoint_for_id
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import token_cost_microdollars


def _client_and_key() -> tuple[TestClient, dict[str, object]]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "openai-priority@example.com"},
        json={"name": "openai priority"},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def _authorize(
    client: TestClient,
    key: dict[str, object],
    *,
    model: str = "openai/gpt-5.6-sol",
    service_tier: str = "priority",
    input_tokens: int = 1_000,
    output_tokens: int = 1_000,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "api_key_hash": key["hash"],
        "model": model,
        "service_tier": service_tier,
        "estimated_input_tokens": input_tokens,
        "max_output_tokens": output_tokens,
    }
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    response = client.post("/v1/internal/gateway/authorize", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_hourly_openai_parser_captures_announced_standard_price_cuts() -> None:
    parsed = parse_openai_pricing(
        "\n".join(
            (
                "| Model | Input | Cached input | Cache writes | Output | "
                "Input | Cached input | Cache writes | Output |",
                "| gpt-5.6-luna | $0.20 | $0.02 | $0.25 | $1.20 | $0.40 | $0.04 | $0.50 | $1.80 |",
                "| gpt-5.6-terra | $2.00 | $0.20 | $2.50 | $12.00 | "
                "$4.00 | $0.40 | $5.00 | $18.00 |",
                "| gpt-5.6-sol | $5.00 | $0.50 | $6.25 | $30.00 | "
                "$10.00 | $1.00 | $12.50 | $45.00 |",
            )
        )
    )

    assert parsed["openai/gpt-5.6-luna"]["tiers"][0] == {
        "max_prompt_tokens": 272_000,
        "prompt_micro_per_m": 200_000,
        "completion_micro_per_m": 1_200_000,
        "prompt_cached_micro_per_m": 20_000,
    }
    assert parsed["openai/gpt-5.6-terra"]["tiers"][0] == {
        "max_prompt_tokens": 272_000,
        "prompt_micro_per_m": 2_000_000,
        "completion_micro_per_m": 12_000_000,
        "prompt_cached_micro_per_m": 200_000,
    }


def test_priority_authorization_uses_only_openai_and_reserves_priority_price() -> None:
    client, key = _client_and_key()
    auth = _authorize(client, key)

    assert auth["provider"] == "openai"
    assert {route["provider"] for route in auth["route_candidates"]} == {"openai"}
    expected = token_cost_microdollars(1_000, 10_500_000) + token_cost_microdollars(
        1_000, 63_000_000
    )
    assert auth["estimated_cost_microdollars"] == expected


def test_priority_settlement_bills_actual_returned_tier() -> None:
    client, key = _client_and_key()

    downgraded = _authorize(client, key, idempotency_key="priority-downgraded")
    default_settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": downgraded["authorization_id"],
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 1_000,
            "service_tier": "default",
            "request_id": "priority-downgraded",
            "elapsed_seconds": 1.0,
        },
    )
    assert default_settle.status_code == 200, default_settle.text
    endpoint = endpoint_for_id(str(downgraded["endpoint_id"]))
    assert endpoint is not None
    expected_default = token_cost_microdollars(
        1_000, endpoint.prompt_price_microdollars_per_million_tokens
    ) + token_cost_microdollars(1_000, endpoint.completion_price_microdollars_per_million_tokens)
    assert default_settle.json()["data"]["cost_microdollars"] == expected_default

    priority = _authorize(client, key, idempotency_key="priority-served")
    priority_settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": priority["authorization_id"],
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 1_000,
            "service_tier": "priority",
            "request_id": "priority-served",
            "elapsed_seconds": 1.0,
        },
    )
    assert priority_settle.status_code == 200, priority_settle.text
    expected_priority = token_cost_microdollars(1_000, 10_500_000) + token_cost_microdollars(
        1_000, 63_000_000
    )
    assert priority_settle.json()["data"]["cost_microdollars"] == expected_priority
    assert expected_priority > expected_default


def test_priority_cached_tokens_use_priority_cached_rate() -> None:
    client, key = _client_and_key()
    auth = _authorize(client, key, idempotency_key="priority-cache")
    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            # OpenAI prompt_tokens includes its cached subset.
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 100,
            "cache_read_input_tokens": 900,
            "service_tier": "priority",
            "request_id": "priority-cache",
            "elapsed_seconds": 1.0,
        },
    )
    assert settled.status_code == 200, settled.text
    expected = (
        token_cost_microdollars(100, 10_500_000)
        + token_cost_microdollars(900, 1_050_000)
        + token_cost_microdollars(100, 63_000_000)
    )
    assert settled.json()["data"]["cost_microdollars"] == expected


def test_priority_rejects_unsupported_models_and_long_context() -> None:
    client, key = _client_and_key()
    unsupported = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "anthropic/claude-haiku-4.5",
            "service_tier": "priority",
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        },
    )
    assert unsupported.status_code == 400

    too_long = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-5.6-sol",
            "service_tier": "priority",
            "estimated_input_tokens": 272_001,
            "max_output_tokens": 10,
        },
    )
    assert too_long.status_code == 400


def test_priority_limit_includes_custom_model_hidden_prompt() -> None:
    client, key = _client_and_key()
    created = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "openai-priority@example.com"},
        json={
            "name": "Priority prompt",
            "base_model_id": "openai/gpt-5.6-sol",
            "hidden_prompt": "12345678",
        },
    )
    assert created.status_code == 201, created.text

    too_long = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": created.json()["data"]["id"],
            "service_tier": "priority",
            "estimated_input_tokens": 271_999,
            "max_output_tokens": 1,
        },
    )
    assert too_long.status_code == 400
    assert "272000" in too_long.text


def test_auto_reserves_priority_but_settles_reported_default() -> None:
    client, key = _client_and_key()
    auth = _authorize(
        client,
        key,
        service_tier="auto",
        idempotency_key="auto-downgrade",
    )
    priority_estimate = token_cost_microdollars(1_000, 10_500_000) + token_cost_microdollars(
        1_000, 63_000_000
    )
    assert auth["estimated_cost_microdollars"] == priority_estimate

    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 1_000,
            "service_tier": "default",
            "request_id": "auto-downgrade",
            "elapsed_seconds": 1.0,
        },
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["cost_microdollars"] < priority_estimate


def test_invalid_requested_and_actual_service_tiers_are_rejected() -> None:
    client, key = _client_and_key()
    invalid_request = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-5.6-sol",
            "service_tier": "flex",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )
    assert invalid_request.status_code == 400

    auth = _authorize(client, key, idempotency_key="invalid-settle-tier")
    invalid_settlement = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 1,
            "actual_output_tokens": 1,
            "service_tier": "auto",
            "request_id": "invalid-settle-tier",
            "elapsed_seconds": 1.0,
        },
    )
    assert invalid_settlement.status_code == 400


def test_service_tier_is_part_of_idempotency_fingerprint() -> None:
    client, key = _client_and_key()
    _authorize(client, key, idempotency_key="tier-fingerprint")
    mismatch = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-5.6-sol",
            "service_tier": "default",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
            "idempotency_key": "tier-fingerprint",
        },
    )
    assert mismatch.status_code == 409


def test_catalog_advertises_openai_priority_pricing() -> None:
    client, _key = _client_and_key()
    response = client.get("/v1/models/openai/gpt-5.6-sol/endpoints")
    assert response.status_code == 200
    openai_endpoint = next(item for item in response.json()["data"] if item["provider"] == "openai")
    assert "service_tier" in openai_endpoint["supported_parameters"]
    assert openai_endpoint["trustedrouter"]["service_tiers"] == [
        "default",
        "auto",
        "priority",
    ]
    assert openai_endpoint["trustedrouter"]["priority_pricing"] == {
        "prompt_microdollars_per_million_tokens": 10_500_000,
        "cached_prompt_microdollars_per_million_tokens": 1_050_000,
        "completion_microdollars_per_million_tokens": 63_000_000,
        "max_prompt_tokens": 272_000,
    }


def test_catalog_does_not_advertise_unpriced_priority_tiers() -> None:
    client, _key = _client_and_key()
    response = client.get("/v1/models/openai/gpt-4o-mini/endpoints")
    assert response.status_code == 200
    openai_endpoint = next(
        item for item in response.json()["data"] if item["provider"] == "openai"
    )

    assert openai_endpoint["trustedrouter"]["service_tiers"] == ["default"]
    assert "priority_pricing" not in openai_endpoint["trustedrouter"]
