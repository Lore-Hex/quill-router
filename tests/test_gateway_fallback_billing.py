from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.storage import STORE


def _client_and_key() -> tuple[TestClient, dict]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "gateway fallback", "include_byok_in_limit": True},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def test_gateway_settle_can_bill_authorized_fallback_model() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="mistral",
        secret_ref="env://MISTRAL_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="mis...1234",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "anthropic/claude-opus-4.7",
            "models": ["mistral/mistral-small-2603"],
            "estimated_input_tokens": 20_000,
            "max_output_tokens": 10_000,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]
    assert auth_data["model"] == "anthropic/claude-opus-4.7"
    assert auth_data["limit_usage_type"] == "Credits"
    assert [item["model"] for item in auth_data["route_candidates"]][:2] == [
        "anthropic/claude-opus-4.7",
        "mistral/mistral-small-2603",
    ]
    assert auth_data["route_candidates"][1]["byok_secret_ref"] == "env://MISTRAL_API_KEY"  # noqa: S105

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth_data["authorization_id"],
            "selected_model": "mistral/mistral-small-2603",
            "actual_input_tokens": 12_345,
            "actual_output_tokens": 6_789,
            "request_id": "gw-fallback-mistral",
            "elapsed_seconds": 1.5,
        },
    )

    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]
    expected_cost = cost_microdollars(MODELS["mistral/mistral-small-2603"], 12_345, 6_789)
    assert data["model"] == "mistral/mistral-small-2603"
    assert data["provider"] == "mistral"
    assert data["usage_type"] == "BYOK"
    assert data["limit_usage_type"] == "Credits"
    assert data["cost_microdollars"] == expected_cost

    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    assert generation.model == "mistral/mistral-small-2603"
    assert generation.usage_type == "BYOK"
    assert generation.total_cost_microdollars == expected_cost

    credit = STORE.get_credit_account(key["workspace_id"])
    refreshed_key = STORE.get_key_by_hash(key["hash"])
    assert credit is not None
    assert refreshed_key is not None
    assert credit.reserved_microdollars == 0
    assert credit.total_usage_microdollars == 0
    assert refreshed_key.reserved_microdollars == 0
    assert refreshed_key.usage_microdollars == 0
    assert refreshed_key.byok_usage_microdollars == expected_cost


def test_gateway_settle_rejects_unlisted_fallback_without_charge_or_generation() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 500,
            "max_output_tokens": 200,
        },
    )
    assert authorize.status_code == 200, authorize.text

    rejected = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorize.json()["data"]["authorization_id"],
            "model": "cerebras/llama3.1-8b",
            "actual_input_tokens": 500,
            "actual_output_tokens": 200,
        },
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["type"] == "bad_request"
    assert not STORE.generation_store.generations
    credit = STORE.get_credit_account(key["workspace_id"])
    refreshed_key = STORE.get_key_by_hash(key["hash"])
    assert credit is not None and credit.total_usage_microdollars == 0
    assert refreshed_key is not None
    assert refreshed_key.usage_microdollars == 0
    assert refreshed_key.byok_usage_microdollars == 0


def test_gateway_refund_records_provider_benchmark_without_generation() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="deepseek",
        secret_ref="env://DEEPSEEK_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="dee...1234",
    )
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "deepseek/deepseek-v4-flash",
            "estimated_input_tokens": 500,
            "max_output_tokens": 200,
            "region": "europe-west4",
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]

    refund = client.post(
        "/v1/internal/gateway/refund",
        json={
            "authorization_id": auth_data["authorization_id"],
            "selected_model": "deepseek/deepseek-v4-flash",
            "actual_input_tokens": 500,
            "actual_output_tokens": 0,
            "elapsed_seconds": 0.4,
            "streamed": True,
            "error_status": 503,
            "error_type": "provider_error",
        },
    )

    assert refund.status_code == 200, refund.text
    assert not STORE.generation_store.generations
    samples = STORE.provider_benchmark_samples()
    assert len(samples) == 1
    sample = samples[0]
    assert sample.status == "error"
    assert sample.model == "deepseek/deepseek-v4-flash"
    assert sample.provider == "deepseek"
    assert sample.region == "europe-west4"
    assert sample.error_status == 503
    assert sample.error_type == "provider_error"
    assert sample.total_cost_microdollars == 0
    assert sample.streamed is True


def test_gateway_missing_byok_primary_skips_to_prepaid_candidate() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "mistral/mistral-small-2603",
            "models": ["anthropic/claude-opus-4.7"],
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]
    assert auth_data["model"] == "anthropic/claude-opus-4.7"
    assert auth_data["usage_type"] == "Credits"
    assert auth_data["limit_usage_type"] == "Credits"
    assert auth_data["credit_reservation_id"]
    assert [item["model"] for item in auth_data["route_candidates"]] == [
        "anthropic/claude-opus-4.7"
    ]

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth_data["authorization_id"],
            "selected_model": "anthropic/claude-opus-4.7",
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 1_000,
            "request_id": "gw-fallback-credit",
        },
    )

    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]
    expected_cost = cost_microdollars(MODELS["anthropic/claude-opus-4.7"], 1_000, 1_000)
    assert data["model"] == "anthropic/claude-opus-4.7"
    assert data["usage_type"] == "Credits"
    assert data["cost_microdollars"] == expected_cost

    credit = STORE.get_credit_account(key["workspace_id"])
    refreshed_key = STORE.get_key_by_hash(key["hash"])
    assert credit is not None
    assert refreshed_key is not None
    assert credit.reserved_microdollars == 0
    assert credit.total_usage_microdollars == expected_cost
    assert refreshed_key.usage_microdollars == expected_cost
    assert refreshed_key.byok_usage_microdollars == 0


def test_gateway_byok_only_without_workspace_config_is_rejected() -> None:
    client, key = _client_and_key()

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "mistral/mistral-small-2603",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )

    assert authorize.status_code == 400
    assert authorize.json()["error"]["type"] == "provider_not_supported"
    assert not STORE.api_keys.gateway_authorizations
    assert not STORE.api_keys.reservations


def test_gateway_prepaid_route_does_not_return_byok_secret_even_if_configured() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="kimi",
        secret_ref="env://KIMI_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="kim...1234",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "kimi/kimi-k2.6",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["usage_type"] == "Credits"
    assert data["byok_secret_ref"] is None
    assert data["byok_key_hint"] is None
    assert data["route_candidates"][0]["byok_secret_ref"] is None
    assert [item["usage_type"] for item in data["route_candidates"]] == ["Credits", "BYOK"]


def test_gateway_can_prefer_byok_endpoint_for_dual_mode_model() -> None:
    client, key = _client_and_key()
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"],
        provider="kimi",
        secret_ref="env://KIMI_API_KEY",  # noqa: S106 - provider secret ref, not a secret.
        key_hint="kim...1234",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "kimi/kimi-k2.6",
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["usage_type"] == "BYOK"
    assert data["limit_usage_type"] == "BYOK"
    assert data["credit_reservation_id"] is None
    assert data["byok_secret_ref"] == "env://KIMI_API_KEY"  # noqa: S105
    assert data["byok_key_hint"] == "kim...1234"
    assert data["route_candidates"] == [
        {
            "endpoint_id": data["endpoint_id"],
            "model": "kimi/kimi-k2.6",
            "provider": "kimi",
            "provider_name": "Kimi",
            "usage_type": "BYOK",
            "byok_secret_ref": "env://KIMI_API_KEY",
            "byok_key_hint": "kim...1234",
            "region": "us-central1",
        }
    ]

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": data["authorization_id"],
            "selected_endpoint": data["endpoint_id"],
            "actual_input_tokens": 2_000,
            "actual_output_tokens": 1_000,
            "request_id": "gw-kimi-byok",
        },
    )

    assert settle.status_code == 200, settle.text
    settled = settle.json()["data"]
    assert settled["usage_type"] == "BYOK"
    assert settled["provider"] == "kimi"
    generation = STORE.get_generation(settled["generation_id"])
    assert generation is not None
    assert generation.usage_type == "BYOK"
    assert generation.provider == "kimi"
