from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.catalog import endpoint_for_id
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal import gateway as gateway_routes
from trusted_router.storage import STORE


def _client_and_key() -> tuple[TestClient, dict]:
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "native-batch@example.com"},
        json={"name": "native batch"},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def test_native_batch_discount_is_integer_exact_and_never_rounds_positive_to_zero() -> None:
    assert gateway_routes._NATIVE_BATCH_DISCOUNT_BPS == {
        "openai": 5_000,
        "parasail": 5_000,
    }
    assert gateway_routes._native_batch_cost_or_error(0, route_type=None, provider="openai") == 0
    assert (
        gateway_routes._native_batch_cost_or_error(
            9, route_type="chat.completions", provider="openai"
        )
        == 9
    )
    assert (
        gateway_routes._native_batch_cost_or_error(
            1,
            route_type="batch.native.chat.completions",
            provider="openai",
            idempotency_key="tr-native-batch:test:0",
        )
        == 1
    )
    assert (
        gateway_routes._native_batch_cost_or_error(
            9,
            route_type="batch.native.embeddings",
            provider="parasail",
            idempotency_key="tr-native-batch:test:0",
        )
        == 5
    )


def test_native_batch_authorization_outlives_provider_completion_window() -> None:
    assert gateway_routes._authorization_ttl_seconds("chat.completions") == 7_200
    assert gateway_routes._authorization_ttl_seconds("batch.native.chat.completions") == 93_600


def test_native_batch_authorization_replays_across_region_and_estimator_drift() -> None:
    client, key = _client_and_key()
    idempotency_key = "tr-native-batch:test-cross-region-replay:0"
    base = {
        "api_key_hash": key["hash"],
        "model": "openai/gpt-5.5",
        "max_tokens": 100,
        "provider": {"only": ["openai"], "usage": "credits"},
        "route_type": "batch.native.chat.completions",
        "idempotency_key": idempotency_key,
    }
    first = client.post(
        "/v1/internal/gateway/authorize",
        json={
            **base,
            "region": "us-central1",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 100,
        },
    )
    assert first.status_code == 200, first.text

    replay = client.post(
        "/v1/internal/gateway/authorize",
        json={
            **base,
            "region": "europe-west4",
            "estimated_input_tokens": 1_017,
            "max_output_tokens": 121,
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["authorization_id"] == first.json()["data"]["authorization_id"]
    assert replay.json()["data"]["idempotent_replay"] is True


def test_native_batch_retention_policy_fails_closed() -> None:
    settings = Settings(environment="test")
    allowed = [
        {"model": "openai/gpt-5.5"},
        {
            "model": "openai/gpt-5.5",
            "provider": {
                "only": ["openai"],
                "usage": "credits",
                "data_collection": "allow",
            },
        },
    ]
    blocked = [
        {"model": "openai/gpt-5.5", "models": ["openai/gpt-5.5"]},
        {"model": "openai/gpt-5.5", "service_tier": "priority"},
        {"model": "openai/gpt-5.5", "store": False},
        {"model": "openai/gpt-5.5", "e2e": True},
        {"model": "openai/gpt-5.5", "provider": {"data_collection": "deny"}},
        {"model": "openai/gpt-5.5", "provider": {"min_privacy": "zdr"}},
        {"model": "openai/gpt-5.5", "provider": {"jurisdiction": "eu"}},
        {"model": "openai/gpt-5.5", "provider": {"usage": "byok"}},
        {"model": "openai/gpt-5.5", "provider": {"future_privacy_mode": "strict"}},
        {"model": "openai/gpt-5.5:zdr"},
        {"model": "openai/gpt-5.5:e2e"},
    ]
    assert all(
        gateway_routes._native_batch_request_allows_retention(body, settings) for body in allowed
    )
    assert not any(
        gateway_routes._native_batch_request_allows_retention(body, settings) for body in blocked
    )


def test_native_batch_settlement_charges_half_and_replay_returns_same_cost() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-5.5",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 500,
            "provider": {"only": ["openai"], "usage": "credits"},
            "route_type": "batch.native.chat.completions",
            "idempotency_key": "tr-native-batch:test-half-price:0",
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    assert auth["native_batch_eligible"] is True
    endpoint = endpoint_for_id(auth["endpoint_id"])
    assert endpoint is not None and endpoint.provider == "openai"
    full_cost = gateway_routes._endpoint_cost_microdollars(endpoint, 800, 200)
    expected = max(1, (full_cost * 5_000 + 9_999) // 10_000)

    settle_body = {
        "authorization_id": auth["authorization_id"],
        "actual_input_tokens": 800,
        "actual_output_tokens": 200,
        "selected_model": auth["model"],
        "selected_endpoint": auth["endpoint_id"],
        "route_type": "batch.native.chat.completions",
        "request_id": "native-batch-result-1",
        "elapsed_seconds": 0.001,
    }
    settled = client.post("/v1/internal/gateway/settle", json=settle_body)
    assert settled.status_code == 200, settled.text
    settled_data = settled.json()["data"]
    assert settled_data["cost_microdollars"] == expected
    assert settled_data["finalization_outcome"] == "settled"

    # Bigtable activity is an optional mirror. Losing it after the Spanner
    # money commit must not make a late refund look like the winning outcome.
    STORE.generation_store.generations.pop(settled_data["generation_id"], None)

    replay = client.post("/v1/internal/gateway/settle", json=settle_body)
    assert replay.status_code == 200, replay.text
    replay_data = replay.json()["data"]
    assert replay_data["already_settled"] is True
    assert replay_data["finalization_outcome"] == "settled"
    assert replay_data["cost_microdollars"] == expected
    assert replay_data["provider"] == "openai"
    assert replay_data["input_tokens"] == 800
    assert replay_data["output_tokens"] == 200
    assert replay_data["reasoning_tokens"] == 0
    assert replay_data["cache_read_input_tokens"] == 0

    late_refund = client.post("/v1/internal/gateway/refund", json=settle_body)
    assert late_refund.status_code == 200, late_refund.text
    late_refund_data = late_refund.json()["data"]
    assert late_refund_data["already_settled"] is True
    assert late_refund_data["finalization_outcome"] == "settled"
    assert late_refund_data["generation_id"] == settled_data["generation_id"]
    assert late_refund_data["cost_microdollars"] == expected
    assert late_refund_data["input_tokens"] == 800
    assert late_refund_data["output_tokens"] == 200


def test_native_batch_settlement_rejects_provider_without_verified_discount() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "deepseek/deepseek-v4-flash",
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
            "provider": {"only": ["deepseek"], "usage": "credits"},
            "route_type": "batch.native.chat.completions",
            "idempotency_key": "tr-native-batch:test-unsupported:0",
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    assert auth["native_batch_eligible"] is False
    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 10,
            "actual_output_tokens": 10,
            "selected_model": auth["model"],
            "selected_endpoint": auth["endpoint_id"],
            "route_type": "batch.native.chat.completions",
            "elapsed_seconds": 0.001,
        },
    )
    assert settled.status_code == 400, settled.text
    authorization = STORE.get_gateway_authorization(auth["authorization_id"])
    assert authorization is not None and not authorization.settled


def test_native_batch_refund_replay_releases_hold_once() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-5.5",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 500,
            "provider": {"only": ["openai"], "usage": "credits"},
            "route_type": "batch.native.chat.completions",
            "idempotency_key": "tr-native-batch:test-refund-once:0",
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    refund_body = {
        "authorization_id": auth["authorization_id"],
        "error_status": 503,
        "error_type": "native_batch_fallback",
        "elapsed_seconds": 0.001,
        "route_type": "batch.native.chat.completions",
    }
    first = client.post("/v1/internal/gateway/refund", json=refund_body)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["settled"] is True
    assert first.json()["data"]["finalization_outcome"] == "refunded"

    replay = client.post("/v1/internal/gateway/refund", json=refund_body)
    assert replay.status_code == 200, replay.text
    replay_data = replay.json()["data"]
    assert replay_data["already_settled"] is True
    assert replay_data["finalization_outcome"] == "refunded"
    assert replay_data["cost_microdollars"] == 0
    assert "generation_id" not in replay_data
    authorization = STORE.get_gateway_authorization(auth["authorization_id"])
    assert authorization is not None and authorization.settled


def test_native_batch_eligibility_does_not_override_primary_provider_order() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "deepseek/deepseek-v4-flash",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 500,
            "provider": {"usage": "credits"},
            "route_type": "batch.native.chat.completions",
            "idempotency_key": "tr-native-batch:test-secondary-native:0",
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    primary = endpoint_for_id(auth["endpoint_id"])
    assert primary is not None and primary.provider not in {"openai", "parasail"}
    assert auth["native_batch_eligible"] is False
    assert any(candidate["provider"] == "parasail" for candidate in auth["route_candidates"])

    refunded = client.post(
        "/v1/internal/gateway/refund",
        json={
            "authorization_id": auth["authorization_id"],
            "error_status": 503,
            "error_type": "native_batch_fallback",
            "elapsed_seconds": 0.001,
            "route_type": "batch.native.chat.completions",
        },
    )
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["data"]["cost_microdollars"] == 0


def test_ordinary_authorization_cannot_claim_native_batch_discount_at_settlement() -> None:
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-5.5",
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
            "provider": {"only": ["openai"], "usage": "credits"},
            "route_type": "chat.completions",
            "idempotency_key": "ordinary-request",
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    assert auth["native_batch_eligible"] is False
    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 10,
            "actual_output_tokens": 10,
            "selected_model": auth["model"],
            "selected_endpoint": auth["endpoint_id"],
            "route_type": "batch.native.chat.completions",
            "elapsed_seconds": 0.001,
        },
    )
    assert settled.status_code == 400, settled.text
    authorization = STORE.get_gateway_authorization(auth["authorization_id"])
    assert authorization is not None and not authorization.settled
