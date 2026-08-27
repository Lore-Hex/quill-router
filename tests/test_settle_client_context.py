from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.routes.internal import gateway as gateway_routes
from trusted_router.storage import STORE
from trusted_router.storage_models import Generation, generation_id_for_authorization
from trusted_router.storage_operational_analytics import activity_payload

GATEWAY_REQUEST_ID = f"rlog_{'a' * 32}"
VALID_CLIENT_CONTEXT: dict[str, Any] = {
    "v": 1,
    "source": "tr",
    "sdk": "tr-py",
    "sdk_version": "1.23.456+retry.1",
    "lang": "python",
    "runtime": "python/3.12.4",
    "os": "macos",
    "arch": "arm64",
    "timeout_ms": 120_000,
    "attempt": 2,
    "prev_outcome": "transport_error",
    "prev_error_class": "connect_timeout",
    "prev_host": "ally",
    "prev_elapsed_ms": 1_500,
    "since_first_ms": 3_250,
    "stream": True,
    "failover_used": True,
}
CLIENT_GENERATION_FIELDS = (
    "client_source",
    "client_sdk",
    "client_sdk_version",
    "client_lang",
    "client_runtime",
    "client_os",
    "client_arch",
    "client_timeout_ms",
    "client_attempt",
    "client_prev_outcome",
    "client_prev_error_class",
    "client_prev_host",
    "client_prev_elapsed_ms",
    "client_since_first_ms",
    "client_stream",
    "client_failover_used",
)
EXPECTED_CLIENT_GENERATION = {
    "client_source": "tr",
    "client_sdk": "tr-py",
    "client_sdk_version": "1.23.456+retry.1",
    "client_lang": "python",
    "client_runtime": "python/3.12.4",
    "client_os": "macos",
    "client_arch": "arm64",
    "client_timeout_ms": 120_000,
    "client_attempt": 2,
    "client_prev_outcome": "transport_error",
    "client_prev_error_class": "connect_timeout",
    "client_prev_host": "ally",
    "client_prev_elapsed_ms": 1_500,
    "client_since_first_ms": 3_250,
    "client_stream": True,
    "client_failover_used": True,
}


def _create_key(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "client-context@example.com"},
        json={"name": "client context"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


def _authorize(
    client: TestClient,
    key: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "api_key_hash": key["hash"],
        "model": "openai/gpt-5.5",
        "estimated_input_tokens": 100,
        "max_output_tokens": 20,
    }
    if extra:
        body.update(extra)
    response = client.post("/v1/internal/gateway/authorize", json=body)
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def _settle(
    client: TestClient,
    authorization: dict[str, Any],
    *,
    client_context: dict[str, Any] | None = None,
    gateway_request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[Any, Generation]:
    body: dict[str, Any] = {
        "authorization_id": authorization["authorization_id"],
        "actual_input_tokens": 12,
        "actual_output_tokens": 8,
        "request_id": "provider-request-id",
        "finish_reason": "stop",
        "status": "success",
        "elapsed_seconds": 0.5,
    }
    if client_context is not None:
        body["client"] = client_context
    if gateway_request_id is not None:
        body["gateway_request_id"] = gateway_request_id
    if extra:
        body.update(extra)
    response = client.post("/v1/internal/gateway/settle", json=body)
    assert response.status_code == 200, response.text
    generation = STORE.get_generation(response.json()["data"]["generation_id"])
    assert generation is not None
    return response, generation


def _generation_from_body(
    authorization: Any,
    body: dict[str, Any],
) -> Generation:
    return Generation.from_settle_body(
        authorization=authorization,
        provider_name="OpenAI",
        model_id=authorization.model_id,
        usage_type=authorization.usage_type,
        provider=authorization.provider,
        body=body,
        input_tokens=12,
        output_tokens=8,
        actual_cost_microdollars=42,
    )


def test_valid_client_context_is_stored_and_emitted_to_activity(client: TestClient) -> None:
    key = _create_key(client)
    authorization = _authorize(client, key)

    _response, generation = _settle(
        client,
        authorization,
        client_context=VALID_CLIENT_CONTEXT,
        gateway_request_id=GATEWAY_REQUEST_ID,
    )

    assert generation.gateway_request_id == GATEWAY_REQUEST_ID
    assert generation.synthetic is False
    for field_name, expected in EXPECTED_CLIENT_GENERATION.items():
        assert getattr(generation, field_name) == expected

    payload = activity_payload(generation)
    assert payload["gateway_request_id"] == GATEWAY_REQUEST_ID
    assert payload["synthetic"] is False
    for field_name, expected in EXPECTED_CLIENT_GENERATION.items():
        assert payload[field_name] == expected


@pytest.mark.parametrize(
    "garbage",
    [
        {"attempt": {"wrong": "type"}},
        {"source": "unknown"},
        {"extra": "not allowed"},
        {"sdk_version": "x" * 5_000},
        {"runtime": {"nested": True}},
    ],
    ids=["wrong-types", "unknown-enum", "extra-key", "five-kb-string", "nested-dict"],
)
def test_invalid_client_context_is_dropped_without_failing_settlement(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    garbage: dict[str, Any],
) -> None:
    key = _create_key(client)
    authorization = _authorize(client, key)

    with caplog.at_level(logging.WARNING, logger=gateway_routes.__name__):
        response, generation = _settle(
            client,
            authorization,
            client_context=garbage,
            gateway_request_id=GATEWAY_REQUEST_ID,
        )

    assert response.status_code == 200
    assert generation.request_id == "provider-request-id"
    assert generation.tokens_prompt == 12
    assert generation.tokens_completion == 8
    assert generation.finish_reason == "stop"
    assert generation.status == "success"
    assert generation.gateway_request_id == GATEWAY_REQUEST_ID
    assert all(getattr(generation, field_name) is None for field_name in CLIENT_GENERATION_FIELDS)
    payload = activity_payload(generation)
    assert all(payload[field_name] is None for field_name in CLIENT_GENERATION_FIELDS)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("invalid gateway settlement client context dropped")
    ]
    assert warnings == [
        "invalid gateway settlement client context dropped "
        f"authorization_id={authorization['authorization_id']} error_class=ValidationError"
    ]


@pytest.mark.parametrize(
    ("gateway_request_id", "expected"),
    [
        (GATEWAY_REQUEST_ID, GATEWAY_REQUEST_ID),
        (f"rlog_{'A' * 32}", None),
    ],
    ids=["valid", "invalid-shape"],
)
def test_gateway_request_id_is_stored_only_when_shape_is_valid(
    client: TestClient,
    gateway_request_id: str,
    expected: str | None,
) -> None:
    key = _create_key(client)
    authorization = _authorize(client, key)

    _response, generation = _settle(
        client,
        authorization,
        gateway_request_id=gateway_request_id,
    )

    assert generation.gateway_request_id == expected
    assert activity_payload(generation)["gateway_request_id"] == expected


def test_broadcast_excludes_client_but_keeps_gateway_request_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _create_key(client)
    authorization = _authorize(client, key)
    captured: dict[str, Any] = {}

    def capture_broadcast(generation: Generation, *, settle_body: dict[str, Any]) -> None:
        captured["generation"] = generation
        captured["settle_body"] = settle_body

    monkeypatch.setattr(gateway_routes, "enqueue_metadata_broadcast", capture_broadcast)

    _response, generation = _settle(
        client,
        authorization,
        client_context=VALID_CLIENT_CONTEXT,
        gateway_request_id=GATEWAY_REQUEST_ID,
        extra={"app_id": "forged-app"},
    )

    assert captured["generation"] is generation
    assert "client" not in captured["settle_body"]
    assert "app_id" not in captured["settle_body"]
    assert captured["settle_body"]["gateway_request_id"] == GATEWAY_REQUEST_ID
    assert generation.client_sdk == "tr-py"


def test_repair_metadata_keeps_validated_client_context_and_request_id(
    client: TestClient,
) -> None:
    key = _create_key(client)
    authorized = _authorize(client, key)
    authorization = STORE.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None
    settle_body = gateway_routes._settle_body_with_safe_client_context(
        {
            "authorization_id": authorization.id,
            "request_id": "provider-request-id",
            "elapsed_seconds": 0.5,
            "price_tier_input_tokens": 6,
            "client": dict(VALID_CLIENT_CONTEXT),
            "gateway_request_id": GATEWAY_REQUEST_ID,
        },
        authorization.id,
    )

    frozen = gateway_routes._settle_repair_metadata(settle_body)

    assert frozen["client"] == VALID_CLIENT_CONTEXT
    assert frozen["gateway_request_id"] == GATEWAY_REQUEST_ID
    assert "price_tier_input_tokens" not in frozen
    original_generation = _generation_from_body(authorization, settle_body)
    repaired_generation = _generation_from_body(authorization, frozen)
    for field_name in ("gateway_request_id", *CLIENT_GENERATION_FIELDS):
        assert getattr(repaired_generation, field_name) == getattr(original_generation, field_name)


def test_refund_logs_validated_client_context_without_writing_generation(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = _create_key(client)
    authorization = _authorize(client, key)
    authorization_id = authorization["authorization_id"]

    with caplog.at_level(logging.INFO, logger=gateway_routes.__name__):
        response = client.post(
            "/v1/internal/gateway/refund",
            json={
                "authorization_id": authorization_id,
                "actual_input_tokens": 12,
                "actual_output_tokens": 8,
                "elapsed_seconds": 0.5,
                "client": VALID_CLIENT_CONTEXT,
                "gateway_request_id": GATEWAY_REQUEST_ID,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["generation_id"] is None
    assert STORE.get_generation(generation_id_for_authorization(authorization_id)) is None
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("gateway.refund_client_context")
    ]
    assert messages == [
        "gateway.refund_client_context "
        f"authorization_id={authorization_id} gateway_request_id={GATEWAY_REQUEST_ID} "
        "sdk=tr-py sdk_version=1.23.456+retry.1 attempt=2 "
        "prev_outcome=transport_error prev_error_class=connect_timeout "
        "prev_host=ally failover_used=True"
    ]


def test_authorize_with_client_extra_has_unchanged_response_shape(client: TestClient) -> None:
    key = _create_key(client)

    without_client = _authorize(client, key)
    with_client = _authorize(client, key, extra={"client": VALID_CLIENT_CONTEXT})

    for response in (without_client, with_client):
        response.pop("authorization_id")
        response.pop("credit_reservation_id")
    assert with_client == without_client


def test_old_generation_payload_loads_with_client_context_defaults() -> None:
    old_payload: dict[str, Any] = {
        "id": "gen-old",
        "request_id": "req-old",
        "workspace_id": "ws-old",
        "key_hash": "key-old",
        "model": "openai/gpt-5.5",
        "provider_name": "OpenAI",
        "app": "TrustedRouter Gateway",
        "tokens_prompt": 12,
        "tokens_completion": 8,
        "total_cost_microdollars": 42,
        "usage_type": "Credits",
        "speed_tokens_per_second": 16.0,
        "finish_reason": "stop",
        "status": "success",
        "streamed": False,
    }

    generation = Generation(**old_payload)

    assert generation.gateway_request_id is None
    assert generation.synthetic is False
    assert all(getattr(generation, field_name) is None for field_name in CLIENT_GENERATION_FIELDS)


@pytest.mark.parametrize(
    "body",
    [
        {"metadata": {"trustedrouter_synthetic": "true"}},
        {"app": "TrustedRouter Synthetic"},
    ],
    ids=["metadata", "app"],
)
def test_generation_records_synthetic_explicitly(
    client: TestClient,
    body: dict[str, Any],
) -> None:
    key = _create_key(client)
    authorized = _authorize(client, key)
    authorization = STORE.get_gateway_authorization(authorized["authorization_id"])
    assert authorization is not None

    generation = _generation_from_body(authorization, body)

    assert generation.synthetic is True
    assert generation.app == "TrustedRouter Synthetic"
    assert activity_payload(generation)["synthetic"] is True
