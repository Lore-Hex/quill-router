from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.custom_model_billing import (
    HUMAN_PRICE_MAX_MICRODOLLARS_PER_M,
    MACHINE_PRICE_MAX_MICRODOLLARS_PER_M,
    custom_model_cost_microdollars,
    owner_share_microdollars,
    user_model_payout_event_id,
)
from trusted_router.main import create_app
from trusted_router.provider_types import estimate_tokens_from_messages
from trusted_router.services.user_model_dispatch import (
    BufferedUserModelDispatch,
    dispatch_user_model,
)
from trusted_router.services.user_model_probe import ProbeResult
from trusted_router.services.user_model_secrets import (
    USER_MODEL_ENDPOINT_KEY_PURPOSE,
    USER_MODEL_SECRET_NAMESPACE,
    USER_MODEL_SIGNING_PURPOSE,
)
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_user_models import InMemoryUserProvidedModels

HEADERS = {"x-trustedrouter-user": "user-model-owner@example.com"}


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )


@pytest.fixture
def dispatch_client(test_settings: Settings) -> TestClient:
    """A client whose gateway will authorize/resolve user-provided models.

    The flag ships OFF until the settle/refund half of user-model billing
    exists (Phase 5); the gateway tests below opt in explicitly.
    """
    settings = test_settings.model_copy(update={"user_models_dispatch_enabled": True})
    return TestClient(create_app(settings, init_observability=False))


def _body(
    *,
    slug: str = "community-machine",
    kind: str = "machine",
    display_identity: str = "handle",
    display_name: str = "helpful-operator",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Community model",
        "slug": slug,
        "kind": kind,
        "description": "A model operated by a community member.",
        "display_identity": display_identity,
        "display_name": display_name,
        "endpoint_url": "https://owner.example/v1",
        "upstream_model_id": "owner-model",
        "supports_streaming": True,
        "heartbeat_interval_seconds": 30,
        "max_concurrency": 2,
        "prompt_price_microdollars_per_million_tokens": 100,
        "completion_price_microdollars_per_million_tokens": 200,
    }
    if kind == "human":
        body["prompt_price_microdollars_per_million_tokens"] = 100_000_000_000
        body["completion_price_microdollars_per_million_tokens"] = 100_000_000_000
    return body


def _create(
    client: TestClient,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] = HEADERS,
) -> dict[str, Any]:
    response = client.post("/v1/user-models", headers=headers, json=body or _body())
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _create_key(
    client: TestClient,
    *,
    headers: dict[str, str] = HEADERS,
) -> dict[str, Any]:
    response = client.post("/v1/keys", headers=headers, json={"name": "gateway"})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _online(created: dict[str, Any]) -> None:
    STORE.set_user_model_online(
        created["id"],
        owner_user_id=created["owner_user_id"],
        online=True,
    )


def _authorize(
    client: TestClient,
    key: dict[str, Any],
    model_id: str,
    *,
    idempotency_key: str,
    input_tokens: int = 1_000,
    output_tokens: int = 2_000,
) -> dict[str, Any]:
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": model_id,
            "estimated_input_tokens": input_tokens,
            "max_output_tokens": output_tokens,
            "idempotency_key": idempotency_key,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _settle(
    client: TestClient,
    authorization_id: str,
    *,
    input_tokens: int = 1_000,
    output_tokens: int = 2_000,
) -> Any:
    return client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": input_tokens,
            "actual_output_tokens": output_tokens,
            "elapsed_seconds": 0.2,
            "first_token_seconds": 0.05,
        },
    )


def _refund(
    client: TestClient,
    authorization_id: str,
    *,
    error_status: int | None = None,
    error_type: str | None = None,
) -> Any:
    body: dict[str, Any] = {"authorization_id": authorization_id}
    if error_status is not None:
        body["error_status"] = error_status
    if error_type is not None:
        body["error_type"] = error_type
    return client.post("/v1/internal/gateway/refund", json=body)


def test_user_model_crud_ownership_and_one_time_secrets(client: TestClient) -> None:
    body = _body()
    body["endpoint_api_key"] = "owner-endpoint-api-key"
    created = _create(client, body=body)
    assert created["id"] == "tr-user-model/user-model-owner-community-machine"
    assert created["signing_secret"]
    assert created["endpoint_key_hint"] == "...-key"
    assert "encrypted_endpoint_api_key" not in created
    assert "encrypted_signing_secret" not in created

    listed = client.get("/v1/user-models", headers=HEADERS)
    fetched = client.get(f"/v1/user-models/{created['id']}", headers=HEADERS)
    for response in (listed, fetched):
        assert response.status_code == 200, response.text
        assert "signing_secret" not in response.text
        assert "owner-endpoint-api-key" not in response.text
        assert "encrypted_" not in response.text

    denied = client.get(
        f"/v1/user-models/{created['id']}",
        headers={"x-trustedrouter-user": "other@example.com"},
    )
    assert denied.status_code == 404

    patched = client.patch(
        f"/v1/user-models/{created['id']}",
        headers=HEADERS,
        json={"name": "Updated community model", "description": "Updated"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["name"] == "Updated community model"
    assert patched.json()["data"]["revision"] == 2

    rotated = client.post(
        f"/v1/user-models/{created['id']}/rotate-secrets",
        headers=HEADERS,
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["data"]["signing_secret"] != created["signing_secret"]
    assert rotated.json()["data"]["revision"] == 3
    fetched_again = client.get(f"/v1/user-models/{created['id']}", headers=HEADERS)
    assert "signing_secret" not in fetched_again.text

    deleted = client.delete(f"/v1/user-models/{created['id']}", headers=HEADERS)
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"deleted": True, "id": created["id"]}


@pytest.mark.parametrize("field", ("online", "human_verified", "status", "revision"))
def test_patch_rejects_protected_fields(client: TestClient, field: str) -> None:
    created = _create(client)
    response = client.patch(
        f"/v1/user-models/{created['id']}",
        headers=HEADERS,
        json={field: True if field != "status" else "suspended"},
    )
    assert response.status_code == 400


def test_limit_three_and_distinct_namespaces_allow_matching_slugs(
    client: TestClient,
) -> None:
    wrapper = client.post(
        "/v1/custom-models",
        headers=HEADERS,
        json={
            "name": "Wrapper",
            "slug": "wrapper-first",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "private",
        },
    )
    assert wrapper.status_code == 201, wrapper.text
    matching_user_model = client.post(
        "/v1/user-models", headers=HEADERS, json=_body(slug="wrapper-first")
    )
    assert matching_user_model.status_code == 201, matching_user_model.text
    assert matching_user_model.json()["data"]["id"] == (
        "tr-user-model/user-model-owner-wrapper-first"
    )

    _create(client, body=_body(slug="user-first"))
    matching_wrapper = client.post(
        "/v1/custom-models",
        headers=HEADERS,
        json={
            "name": "Wrapper",
            "slug": "user-first",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "private",
        },
    )
    assert matching_wrapper.status_code == 201, matching_wrapper.text
    assert matching_wrapper.json()["data"]["id"] == (
        "tr-custom-model/user-model-owner-user-first"
    )

    _create(client, body=_body(slug="second-user-model"))
    over_limit = client.post(
        "/v1/user-models", headers=HEADERS, json=_body(slug="fourth-user-model")
    )
    assert over_limit.status_code == 400


def test_verification_gate_applies_to_post_and_patch() -> None:
    settings = Settings(
        environment="test",
        custom_models_require_verification=True,
    )
    client = TestClient(create_app(settings, init_observability=False))
    client.get("/v1/user-models", headers=HEADERS)
    user = STORE.find_user_by_email(HEADERS["x-trustedrouter-user"])
    assert user is not None
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    existing = STORE.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Existing",
        kind="machine",
        display_name="existing-operator",
        endpoint_url="https://owner.example/v1",
        slug="existing-user-model",
    )

    created = client.post("/v1/user-models", headers=HEADERS, json=_body())
    patched = client.patch(
        f"/v1/user-models/{existing.id}", headers=HEADERS, json={"name": "Blocked"}
    )
    for response in (created, patched):
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "verification_required"


def test_clock_in_probe_failure_stays_offline(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create(client)

    async def failed_probe(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=False, detail="Canary response was invalid")

    monkeypatch.setattr("trusted_router.routes.user_models.probe_user_model", failed_probe)
    response = client.post(
        f"/v1/user-models/{created['id']}/clock-in",
        headers=HEADERS,
    )
    assert response.status_code == 409
    stored = STORE.get_user_model(created["id"])
    assert stored is not None
    assert stored.online is False


def test_gateway_rejects_off_clock_user_model_with_stable_error(
    dispatch_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = dispatch_client
    key = _create_key(client)
    created = _create(client)

    with caplog.at_level("WARNING", logger="trusted_router"):
        response = client.post(
            "/v1/internal/gateway/authorize",
            headers={"X-Request-ID": "req-off-the-clock"},
            json={
                "api_key_hash": key["hash"],
                "model": created["id"],
                "estimated_input_tokens": 10,
                "max_output_tokens": 10,
            },
        )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["type"] == "model_off_the_clock"
    assert created["id"] in response.json()["error"]["message"]
    assert "machine" in response.json()["error"]["message"]
    # This 503 counts against the billing-path 5xx alert, so it names itself.
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("billing.authorize_user_model_off_the_clock ")
    ]
    assert len(lines) == 1, lines
    assert f"user_model_id={created['id']}" in lines[0]
    assert "kind=machine" in lines[0]
    assert "request_id=req-off-the-clock" in lines[0]
    assert key["hash"] not in lines[0]


def test_gateway_authorization_freezes_user_model_attribution(
    dispatch_client: TestClient,
) -> None:
    client = dispatch_client
    key = _create_key(client)
    created = _create(client)
    STORE.set_user_model_online(created["id"], owner_user_id=created["owner_user_id"], online=True)

    byok = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": created["id"],
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        },
    )
    assert byok.status_code == 400
    assert byok.json()["error"]["message"] == (
        "User-provided models do not support BYOK routes"
    )

    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": created["id"],
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["requested_model"] == created["id"]
    assert data["model"] == created["id"]
    assert data["usage_type"] == "Credits"
    authorization = STORE.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None
    assert authorization.user_provided_model_id == created["id"]
    assert authorization.user_provided_model_revision == created["revision"]
    assert authorization.user_model_prompt_price_microdollars_per_m == 100
    assert authorization.user_model_completion_price_microdollars_per_m == 200
    assert authorization.user_model_owner_user_id == created["owner_user_id"]


def test_gateway_settle_uses_frozen_prices_and_pays_owner_once(
    dispatch_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = dispatch_client
    payer_headers = {"x-trustedrouter-user": "user-model-payer@example.com"}
    key = _create_key(client, headers=payer_headers)
    body = _body(slug="frozen-billing")
    prompt_price = 2_000_000
    completion_price = 3_000_000
    body["prompt_price_microdollars_per_million_tokens"] = prompt_price
    body["completion_price_microdollars_per_million_tokens"] = completion_price
    created = _create(client, body=body)
    _online(created)
    authorization = _authorize(
        client,
        key,
        created["id"],
        idempotency_key="user-model-frozen-billing",
    )

    patched = client.patch(
        f"/v1/user-models/{created['id']}",
        headers=HEADERS,
        json={
            "prompt_price_microdollars_per_million_tokens": 10 * prompt_price,
            "completion_price_microdollars_per_million_tokens": 10 * completion_price,
        },
    )
    assert patched.status_code == 200, patched.text

    result_calls = 0
    original = InMemoryUserProvidedModels.record_dispatch_result

    def counted_result(
        self: InMemoryUserProvidedModels,
        model_id: str,
        *,
        success: bool,
    ) -> Any:
        nonlocal result_calls
        result_calls += 1
        return original(self, model_id, success=success)

    monkeypatch.setattr(
        InMemoryUserProvidedModels,
        "record_dispatch_result",
        counted_result,
    )

    settled = _settle(client, authorization["authorization_id"])
    assert settled.status_code == 200, settled.text
    actual_cost = custom_model_cost_microdollars(
        input_tokens=1_000,
        output_tokens=2_000,
        prompt_price=prompt_price,
        completion_price=completion_price,
    )
    payout = owner_share_microdollars(actual_cost)
    assert settled.json()["data"]["cost_microdollars"] == actual_cost

    summary = STORE.earnings_summary(created["owner_user_id"])
    assert summary == {
        "total_earned": payout,
        "total_transferred": 0,
        "available": payout,
    }
    movements = STORE.list_credit_movements(f"user:{created['owner_user_id']}")
    assert len(movements) == 1
    movement = movements[0]
    assert movement.movement_id == user_model_payout_event_id(
        authorization["authorization_id"]
    )
    assert movement.kind == "custom_model_payout"
    assert movement.amount_microdollars == payout
    payer = STORE.find_user_by_email(payer_headers["x-trustedrouter-user"])
    assert payer is not None
    payer_workspace = STORE.list_workspaces_for_user(payer.id)[0]
    assert movement.counterparty_account_id == payer_workspace.id
    assert movement.custom_model_id == created["id"]

    generation_id = settled.json()["data"]["generation_id"]
    generation = STORE.get_generation(generation_id)
    assert generation is not None
    assert generation.custom_model_id == created["id"]
    assert generation.operator_cost_microdollars == payout
    assert generation.elapsed_milliseconds == 200
    assert generation.first_token_milliseconds == 50

    replay = _settle(client, authorization["authorization_id"])
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["already_settled"] is True
    assert STORE.earnings_summary(created["owner_user_id"])["total_earned"] == payout
    assert len(STORE.list_credit_movements(f"user:{created['owner_user_id']}")) == 1
    assert result_calls == 1


def test_gateway_refund_releases_hold_without_payout(
    dispatch_client: TestClient,
) -> None:
    client = dispatch_client
    key = _create_key(client)
    body = _body(slug="refund-billing")
    body["prompt_price_microdollars_per_million_tokens"] = 2_000_000
    body["completion_price_microdollars_per_million_tokens"] = 3_000_000
    created = _create(client, body=body)
    _online(created)
    authorization = _authorize(
        client,
        key,
        created["id"],
        idempotency_key="user-model-refund-billing",
    )
    workspace_money = STORE.credit_money[created["owner_workspace_id"]]
    assert workspace_money.reserved_microdollars > 0

    refunded = _refund(client, authorization["authorization_id"])
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["data"]["cost_microdollars"] == 0
    assert workspace_money.reserved_microdollars == 0
    assert workspace_money.total_usage_microdollars == 0
    assert STORE.earnings_summary(created["owner_user_id"])["total_earned"] == 0
    assert STORE.list_credit_movements(f"user:{created['owner_user_id']}") == []


def test_deleted_user_model_still_settles_from_frozen_authorization(
    dispatch_client: TestClient,
) -> None:
    client = dispatch_client
    payer_headers = {"x-trustedrouter-user": "deleted-model-payer@example.com"}
    key = _create_key(client, headers=payer_headers)
    body = _body(slug="deleted-before-settle")
    body["prompt_price_microdollars_per_million_tokens"] = 2_000_000
    body["completion_price_microdollars_per_million_tokens"] = 3_000_000
    created = _create(client, body=body)
    _online(created)
    authorization = _authorize(
        client,
        key,
        created["id"],
        idempotency_key="user-model-deleted-before-settle",
    )
    deleted = client.delete(f"/v1/user-models/{created['id']}", headers=HEADERS)
    assert deleted.status_code == 200, deleted.text

    settled = _settle(client, authorization["authorization_id"])
    assert settled.status_code == 200, settled.text
    payout = owner_share_microdollars(settled.json()["data"]["cost_microdollars"])
    assert payout > 0
    assert STORE.earnings_summary(created["owner_user_id"])["total_earned"] == payout
    movement = STORE.list_credit_movements(f"user:{created['owner_user_id']}")[0]
    assert movement.custom_model_id == created["id"]


def test_user_model_self_usage_nets_owner_to_thirty_percent(
    dispatch_client: TestClient,
) -> None:
    client = dispatch_client
    key = _create_key(client)
    body = _body(slug="self-usage")
    body["prompt_price_microdollars_per_million_tokens"] = 2_000_000
    body["completion_price_microdollars_per_million_tokens"] = 3_000_000
    created = _create(client, body=body)
    _online(created)
    authorization = _authorize(
        client,
        key,
        created["id"],
        idempotency_key="user-model-self-usage",
    )
    settled = _settle(client, authorization["authorization_id"])
    assert settled.status_code == 200, settled.text
    charge = settled.json()["data"]["cost_microdollars"]
    earned = STORE.earnings_summary(created["owner_user_id"])["available"]
    assert earned == owner_share_microdollars(charge)
    assert charge - earned == charge * 3_000 // 10_000


def test_gateway_refund_strikes_only_owner_faults_and_success_resets(
    dispatch_client: TestClient,
) -> None:
    client = dispatch_client
    key = _create_key(client)
    created = _create(client, body=_body(slug="gateway-strikes"))
    _online(created)
    request_number = 0

    def authorization() -> dict[str, Any]:
        nonlocal request_number
        request_number += 1
        return _authorize(
            client,
            key,
            created["id"],
            idempotency_key=f"user-model-strike-{request_number}",
        )

    no_evidence = authorization()
    response = _refund(client, no_evidence["authorization_id"])
    assert response.status_code == 200, response.text
    stored = STORE.get_user_model(created["id"])
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 0

    caller_fault = authorization()
    response = _refund(
        client,
        caller_fault["authorization_id"],
        error_status=422,
        error_type="bad_request",
    )
    assert response.status_code == 200, response.text
    assert STORE.get_user_model(created["id"]).consecutive_dispatch_failures == 0  # type: ignore[union-attr]

    for _ in range(2):
        owner_fault = authorization()
        response = _refund(
            client,
            owner_fault["authorization_id"],
            error_status=503,
        )
        assert response.status_code == 200, response.text
    assert STORE.get_user_model(created["id"]).consecutive_dispatch_failures == 2  # type: ignore[union-attr]

    success = authorization()
    response = _settle(client, success["authorization_id"])
    assert response.status_code == 200, response.text
    stored = STORE.get_user_model(created["id"])
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 0
    assert stored.online is True

    for _ in range(3):
        owner_fault = authorization()
        response = _refund(
            client,
            owner_fault["authorization_id"],
            error_type="user_model_timeout",
        )
        assert response.status_code == 200, response.text
    stored = STORE.get_user_model(created["id"])
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 3
    assert stored.online is False


def test_gateway_user_model_concurrency_slot_releases_on_settle_and_refund(
    dispatch_client: TestClient,
) -> None:
    client = dispatch_client
    key = _create_key(client)
    body = _body(slug="one-at-a-time")
    body["max_concurrency"] = 1
    created = _create(client, body=body)
    _online(created)

    first = _authorize(
        client,
        key,
        created["id"],
        idempotency_key="user-model-capacity-first",
    )
    at_capacity = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": created["id"],
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 2_000,
            "idempotency_key": "user-model-capacity-second",
        },
    )
    assert at_capacity.status_code == 429, at_capacity.text
    assert at_capacity.json()["error"]["type"] == "rate_limited"
    assert at_capacity.json()["error"]["message"] == (
        f"User-provided model {created['id']} is at capacity (1 concurrent)"
    )

    settled = _settle(client, first["authorization_id"])
    assert settled.status_code == 200, settled.text
    third = _authorize(
        client,
        key,
        created["id"],
        idempotency_key="user-model-capacity-third",
    )
    refunded = _refund(client, third["authorization_id"])
    assert refunded.status_code == 200, refunded.text
    fourth = _authorize(
        client,
        key,
        created["id"],
        idempotency_key="user-model-capacity-fourth",
    )
    cleanup = _refund(client, fourth["authorization_id"])
    assert cleanup.status_code == 200, cleanup.text


def test_gateway_resolves_user_model_dispatch_block(dispatch_client: TestClient) -> None:
    client = dispatch_client
    key = _create_key(client)
    body = _body()
    body["endpoint_api_key"] = "owner-dispatch-key"
    created = _create(client, body=body)

    response = client.post(
        "/v1/internal/gateway/resolve-custom-model",
        json={
            "api_key_hash": key["hash"],
            "model": created["id"],
            "route_type": "chat.completions",
        },
    )
    assert response.status_code == 200, response.text
    dispatch = response.json()["data"]["custom_model"]
    assert dispatch["kind"] == "user_provided"
    assert dispatch["id"] == created["id"]
    assert dispatch["endpoint_url"] == "https://owner.example/v1"
    assert dispatch["upstream_model_id"] == "owner-model"
    assert dispatch["revision"] == 1
    assert dispatch["supports_streaming"] is True
    assert dispatch["endpoint_encrypted_secret"] is not None
    assert dispatch["signing_encrypted_secret"] is not None
    assert dispatch["connect_timeout_seconds"] == 10
    assert dispatch["first_byte_timeout_seconds"] == 30
    assert dispatch["idle_timeout_seconds"] == 60
    assert dispatch["total_timeout_seconds"] == 300
    assert "owner-dispatch-key" not in response.text
    # The envelopes are AAD-bound to the OWNER's workspace and per-secret
    # purposes; the block must carry both or the enclave cannot decrypt.
    assert dispatch["owner_workspace_id"] == created["owner_workspace_id"]
    assert dispatch["owner_user_id"] == created["owner_user_id"]
    assert dispatch["user_model_kind"] == "machine"
    assert dispatch["endpoint_secret_purpose"] == USER_MODEL_ENDPOINT_KEY_PURPOSE
    assert dispatch["signing_secret_purpose"] == USER_MODEL_SIGNING_PURPOSE
    assert dispatch["secret_namespace"] == USER_MODEL_SECRET_NAMESPACE


def test_gateway_hides_user_models_until_dispatch_is_enabled(client: TestClient) -> None:
    """Default flag OFF: neither authorize nor resolve admits a user model.

    Without this, authorize would place a credit hold that no settle path
    can release yet.
    """
    key = _create_key(client)
    created = _create(client)
    STORE.set_user_model_online(created["id"], owner_user_id=created["owner_user_id"], online=True)

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": created["id"],
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        },
    )
    assert authorize.status_code == 404, authorize.text
    resolve = client.post(
        "/v1/internal/gateway/resolve-custom-model",
        json={
            "api_key_hash": key["hash"],
            "model": created["id"],
            "route_type": "chat.completions",
        },
    )
    assert resolve.status_code == 404, resolve.text


def test_local_inference_dispatches_user_model_before_frozen_catalog_routing(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create(client)
    STORE.set_user_model_online(
        created["id"],
        owner_user_id=created["owner_user_id"],
        online=True,
    )

    async def fake_dispatch(*_args: Any, **_kwargs: Any) -> BufferedUserModelDispatch:
        return BufferedUserModelDispatch(
            body={
                "id": "chatcmpl-local-user-model",
                "object": "chat.completion",
                "model": created["id"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "owner reply"},
                        "finish_reason": "stop",
                    }
                ],
            },
            first_token_seconds=0.01,
            elapsed_seconds=0.02,
        )

    monkeypatch.setattr("trusted_router.routes.inference.dispatch_user_model", fake_dispatch)
    response = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": created["id"],
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "owner reply"


def test_local_user_model_dispatch_bills_pays_and_refunds_owner_failure(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(slug="local-billing")
    body["supports_streaming"] = False
    body["prompt_price_microdollars_per_million_tokens"] = 2_000_000
    body["completion_price_microdollars_per_million_tokens"] = 3_000_000
    created = _create(client, body=body)

    async def passing_probe(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=True, detail="ok")

    monkeypatch.setattr("trusted_router.routes.user_models.probe_user_model", passing_probe)
    clocked_in = client.post(
        f"/v1/user-models/{created['id']}/clock-in",
        headers=HEADERS,
    )
    assert clocked_in.status_code == 200, clocked_in.text

    async def owner_success(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-local-billing",
                "object": "chat.completion",
                "model": "owner-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "owner billed reply"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 2_000,
                    "total_tokens": 3_000,
                },
            },
        )

    async def success_dispatch(
        model: Any,
        request_body: dict[str, Any],
        settings: Settings,
    ) -> BufferedUserModelDispatch:
        return await dispatch_user_model(
            model,
            request_body,
            settings,
            transport=httpx.MockTransport(owner_success),
        )

    monkeypatch.setattr(
        "trusted_router.routes.inference.dispatch_user_model",
        success_dispatch,
    )
    payer = STORE.find_user_by_email("alice@example.com")
    assert payer is not None
    payer_workspace = STORE.list_workspaces_for_user(payer.id)[0]
    payer_money = STORE.credit_money[payer_workspace.id]
    usage_before = payer_money.total_usage_microdollars
    response = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": created["id"],
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 2_000,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "owner billed reply"
    # The owner reported 1,000 prompt tokens for "hello": more than the caller
    # authorized. The charge is the owner-priced usage capped at the hold the
    # caller's request reserved (estimated prompt + max_tokens at frozen
    # prices) — the payee's meter can never exceed the payer's authorization.
    reported_cost = custom_model_cost_microdollars(
        input_tokens=1_000,
        output_tokens=2_000,
        prompt_price=2_000_000,
        completion_price=3_000_000,
    )
    hold = custom_model_cost_microdollars(
        input_tokens=estimate_tokens_from_messages([{"role": "user", "content": "hello"}]),
        output_tokens=2_000,
        prompt_price=2_000_000,
        completion_price=3_000_000,
    )
    assert reported_cost > hold
    actual_cost = hold
    payout = owner_share_microdollars(actual_cost)
    assert payer_money.total_usage_microdollars - usage_before == actual_cost
    assert payer_money.reserved_microdollars == 0
    assert STORE.earnings_summary(created["owner_user_id"])["total_earned"] == payout
    movement = STORE.list_credit_movements(f"user:{created['owner_user_id']}")[0]
    assert movement.amount_microdollars == payout
    assert movement.counterparty_account_id == payer_workspace.id
    assert movement.custom_model_id == created["id"]
    generation = next(iter(STORE.target.generation_store.generations.values()))
    assert generation.custom_model_id == created["id"]
    assert movement.movement_id == user_model_payout_event_id(generation.id)

    async def owner_failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "owner unavailable"})

    async def failure_dispatch(
        model: Any,
        request_body: dict[str, Any],
        settings: Settings,
    ) -> BufferedUserModelDispatch:
        return await dispatch_user_model(
            model,
            request_body,
            settings,
            transport=httpx.MockTransport(owner_failure),
        )

    monkeypatch.setattr(
        "trusted_router.routes.inference.dispatch_user_model",
        failure_dispatch,
    )
    usage_after_success = payer_money.total_usage_microdollars
    failed = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": created["id"],
            "messages": [{"role": "user", "content": "try again"}],
            "max_tokens": 2_000,
        },
    )
    assert failed.status_code == 502, failed.text
    assert payer_money.total_usage_microdollars == usage_after_success
    assert payer_money.reserved_microdollars == 0
    assert STORE.earnings_summary(created["owner_user_id"])["total_earned"] == payout
    stored = STORE.get_user_model(created["id"])
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 1


def test_public_shape_is_secret_free_and_resolves_operator_identities(
    client: TestClient,
) -> None:
    client.get("/v1/user-models", headers=HEADERS)
    user = STORE.find_user_by_email(HEADERS["x-trustedrouter-user"])
    assert user is not None
    STORE.set_user_identity_status(
        user.id,
        status="approved",
        verified_name="Verified Operator",
    )
    verified_body = _body(
        slug="verified-community",
        display_identity="verified_name",
        display_name="fallback-handle",
    )
    verified_body["endpoint_api_key"] = "private-owner-key"
    verified = _create(client, body=verified_body)
    human = _create(
        client,
        body=_body(
            slug="live-person",
            kind="human",
            display_name="live-helper",
        ),
    )

    response = client.get("/v1/models/user-provided")
    assert response.status_code == 200, response.text
    by_id = {row["id"]: row for row in response.json()["data"]}
    verified_public = by_id[verified["id"]]
    assert verified_public["operator"] == {
        "display": "Verified Operator",
        "identity": "verified_name",
        "human_verified": False,
    }
    assert verified_public["attested"] is False
    assert verified_public["zero_data_retention"] is False
    assert verified_public["privacy_tier"] == "standard"
    assert verified_public["health"] == "ok"
    assert "private-owner-key" not in response.text
    assert "endpoint_url" not in response.text
    assert "encrypted_" not in response.text

    human_public = by_id[human["id"]]
    assert human_public["operator"]["human_verified"] is True
    assert "A live person will read your messages" in human_public["privacy_notice"]

    detail = client.get(f"/models/{human['id']}")
    assert detail.status_code == 200, detail.text
    assert 'meta name="robots" content="noindex"' in detail.text
    assert "operated by a community member, not TrustedRouter" in detail.text
    assert "✓ verified human" in detail.text
    assert "not attested and is not zero-data-retention" in detail.text
    assert "owner.example" not in detail.text

    chat = client.get(f"/user-chat?model={human['id']}")
    assert chat.status_code == 200
    assert "User-provided model" in chat.text


def test_public_user_model_health_reports_dispatch_and_probe_degradation(
    client: TestClient,
) -> None:
    created = _create(client, body=_body(slug="health-signal"))
    STORE.record_user_model_dispatch_result(created["id"], success=False)

    degraded = client.get(f"/v1/models/user-provided/{created['id']}")
    assert degraded.status_code == 200
    assert degraded.json()["data"]["health"] == "degraded"
    page = client.get(f"/models/{created['id']}")
    assert page.status_code == 200
    assert "Degraded" in page.text

    STORE.record_user_model_dispatch_result(created["id"], success=True)
    STORE.record_user_model_probe(
        created["id"],
        status="failed",
        checked_at="2026-08-16T00:00:00Z",
    )
    assert (
        client.get(f"/v1/models/user-provided/{created['id']}").json()["data"][
            "health"
        ]
        == "degraded"
    )


def test_owner_earnings_api_lists_summary_models_movements_and_transfers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create(client, body=_body(slug="earnings-api"))
    owner_user_id = created["owner_user_id"]
    workspace = STORE.list_workspaces_for_user(owner_user_id)[0]
    assert STORE.credit_user_earnings(
        owner_user_id,
        2_500_000,
        "custom_model_payout:earnings-api-auth",
        custom_model_id=created["id"],
        payer_workspace_id=workspace.id,
    )

    response = client.get("/v1/user-models/earnings", headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"] == {
        "total_earned": 2_500_000,
        "total_earned_display": "$2.50",
        "total_transferred": 0,
        "total_transferred_display": "$0.00",
        "available": 2_500_000,
        "available_display": "$2.50",
    }
    assert data["by_model_30d"] == [
        {"model_id": created["id"], "earned_microdollars": 2_500_000}
    ]
    assert data["recent"][0]["kind"] == "custom_model_payout"
    assert data["recent"][0]["amount_display"] == "$2.50"

    summary_staleness: list[bool] = []
    original_summary = InMemoryStore.earnings_summary

    def recording_summary(
        store: InMemoryStore,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, int]:
        summary_staleness.append(allow_stale)
        return original_summary(store, user_id, allow_stale=allow_stale)

    # Patch the backend class, never the module-global STORE proxy.
    monkeypatch.setattr(InMemoryStore, "earnings_summary", recording_summary)

    body = {
        "workspace_id": workspace.id,
        "amount_microdollars": 1_000_000,
        "idempotency_key": "earnings-api-transfer",
    }
    accepted = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json=body,
    )
    assert accepted.status_code == 200, accepted.text
    assert summary_staleness == [False]
    assert accepted.json()["data"] == {
        "deduplicated": False,
        "summary": {
            "total_earned": 2_500_000,
            "total_earned_display": "$2.50",
            "total_transferred": 1_000_000,
            "total_transferred_display": "$1.00",
            "available": 1_500_000,
            "available_display": "$1.50",
        },
    }
    duplicate = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json=body,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["deduplicated"] is True
    assert summary_staleness == [False, False]

    insufficient = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json={"workspace_id": workspace.id, "amount_microdollars": 2_000_000},
    )
    assert insufficient.status_code == 402
    assert insufficient.json()["error"]["type"] == "insufficient_credits"
    assert insufficient.json()["error"]["available"] == 1_500_000
    assert summary_staleness == [False, False, False]


def test_owner_earnings_transfer_hides_membership_and_refuses_invalid_targets(
    client: TestClient,
) -> None:
    _create(client, body=_body(slug="earnings-targets"))
    user = STORE.find_user_by_email(HEADERS["x-trustedrouter-user"])
    assert user is not None
    workspace = STORE.list_workspaces_for_user(user.id)[0]

    for amount in (0, -1):
        invalid = client.post(
            "/v1/user-models/earnings/transfer",
            headers=HEADERS,
            json={"workspace_id": workspace.id, "amount_microdollars": amount},
        )
        assert invalid.status_code == 400

    stranger = STORE.ensure_user("earnings-stranger@example.com")
    stranger_workspace = STORE.list_workspaces_for_user(stranger.id)[0]
    hidden = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json={"workspace_id": stranger_workspace.id, "amount_microdollars": 1},
    )
    assert hidden.status_code == 404

    workspace.federated_home = "https://home.example"
    federated = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json={"workspace_id": workspace.id, "amount_microdollars": 1},
    )
    assert federated.status_code == 400


def test_earnings_console_renders_and_transfers_with_idempotent_flash(
    client: TestClient,
) -> None:
    user = STORE.ensure_user("earnings-console@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="earnings console",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    model = STORE.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Earning model",
        kind="machine",
        display_name="earner",
        endpoint_url="https://owner.example/v1",
        slug="earnings-console",
    )
    assert STORE.credit_user_earnings(
        user.id,
        3_000_000,
        "custom_model_payout:earnings-console-auth",
        custom_model_id=model.id,
        payer_workspace_id=workspace.id,
    )

    page = client.get("/console/earnings")
    assert page.status_code == 200
    assert "You receive 70% of custom model and registered app markup" in page.text
    assert "cash out at least $100 in USD." in page.text
    assert "Routable" not in page.text
    assert "Earning model" in page.text
    assert 'href="/console/earnings" class="sidebar-link active"' in page.text

    form = {
        "workspace_id": workspace.id,
        "amount": "1.25",
        "idempotency_key": "console-transfer",
    }
    accepted = client.post(
        "/console/earnings/transfer",
        data=form,
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/console/earnings?saved=transferred"
    duplicate = client.post(
        "/console/earnings/transfer",
        data=form,
        follow_redirects=False,
    )
    assert duplicate.headers["location"] == "/console/earnings?saved=duplicate"
    assert STORE.earnings_summary(user.id)["available"] == 1_750_000


def test_https_is_required_outside_local_and_test() -> None:
    settings = Settings(
        environment="staging",
        service_surface="control",
        custom_models_require_verification=False,
        attribution_cookie_secret="staging-attribution-" + "a" * 32,
        stripe_webhook_secret="whsec_staging",  # noqa: S106 - test fixture.
        stripe_secret_key="sk_staging",  # noqa: S106 - test fixture.
    )
    client = TestClient(create_app(settings, init_observability=False))
    user = STORE.ensure_user("staging-owner@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_key, _key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="management",
        creator_user_id=user.id,
        management=True,
    )
    body = _body()
    body["endpoint_url"] = "http://owner.example/v1"
    response = client.post(
        "/v1/user-models",
        headers={"authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert response.status_code == 400
    assert "https" in response.json()["error"]["message"]


def test_console_create_and_rotate_show_signing_secret_once(client: TestClient) -> None:
    user = STORE.ensure_user("console-user-model@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="user model console",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    form = {
        "name": "Console community model",
        "slug": "console-community",
        "kind": "machine",
        "description": "Console-created model",
        "display_identity": "handle",
        "display_name": "console-operator",
        "endpoint_url": "https://owner.example/v1",
        "upstream_model_id": "console-upstream",
        "supports_streaming": "true",
        "max_concurrency": "4",
        "prompt_price_microdollars_per_million_tokens": "100",
        "completion_price_microdollars_per_million_tokens": "200",
    }
    created = client.post("/console/user-models", data=form)
    assert created.status_code == 201, created.text
    assert "shown once" in created.text
    model = STORE.get_user_model("tr-user-model/console-user-model-console-community")
    assert model is not None

    ordinary_page = client.get("/console/user-models")
    assert ordinary_page.status_code == 200
    assert "shown once" not in ordinary_page.text
    assert "not attested and not zero-data-retention" in ordinary_page.text
    assert 'href="/console/user-models"' in ordinary_page.text

    rotated = client.post(f"/console/user-models/{model.id}/rotate-secrets")
    assert rotated.status_code == 200
    assert "shown once" in rotated.text


def test_console_verification_disables_user_model_forms() -> None:
    settings = Settings(
        environment="test",
        custom_models_require_verification=True,
    )
    client = TestClient(create_app(settings, init_observability=False))
    user = STORE.ensure_user("blocked-console-user-model@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="blocked user model console",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)

    page = client.get("/console/user-models")
    assert page.status_code == 200
    assert "Verification required" in page.text
    assert '<fieldset class="form-gate" disabled>' in page.text


def test_local_user_model_streaming_bills_from_the_usage_chunk_and_pays_owner(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streaming quadrant of local billing: the owner streams SSE with
    the OpenAI usage-only final chunk; the caller is charged the owner-priced
    usage (capped at the hold), the owner is paid 70%, and no strike lands."""
    body = _body(slug="local-stream-billing")
    body["supports_streaming"] = True
    body["prompt_price_microdollars_per_million_tokens"] = 2_000_000
    body["completion_price_microdollars_per_million_tokens"] = 3_000_000
    created = _create(client, body=body)

    async def passing_probe(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=True, detail="ok")

    monkeypatch.setattr("trusted_router.routes.user_models.probe_user_model", passing_probe)
    assert (
        client.post(f"/v1/user-models/{created['id']}/clock-in", headers=HEADERS).status_code
        == 200
    )

    def chunk(**payload: Any) -> bytes:
        base = {
            "id": "chatcmpl-local-stream",
            "object": "chat.completion.chunk",
            "created": 1_700_000_000,
            "model": "owner-model",
        }
        base.update(payload)
        return b"data: " + json.dumps(base, separators=(",", ":")).encode() + b"\n\n"

    sse = (
        chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": "streamed "}}])
        + chunk(choices=[{"index": 0, "delta": {"content": "reply"}, "finish_reason": "stop"}])
        + chunk(choices=[], usage={"prompt_tokens": 3, "completion_tokens": 20, "total_tokens": 23})
        + b"data: [DONE]\n\n"
    )

    async def owner_stream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    from trusted_router.services.user_model_dispatch import stream_user_model

    def patched_stream(model: Any, request_body: dict[str, Any], settings: Settings) -> Any:
        return stream_user_model(
            model, request_body, settings, transport=httpx.MockTransport(owner_stream)
        )

    monkeypatch.setattr("trusted_router.routes.inference.stream_user_model", patched_stream)
    payer = STORE.find_user_by_email("alice@example.com")
    assert payer is not None
    payer_workspace = STORE.list_workspaces_for_user(payer.id)[0]
    payer_money = STORE.credit_money[payer_workspace.id]
    usage_before = payer_money.total_usage_microdollars

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": created["id"],
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100,
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        raw = b"".join(response.iter_bytes())
    assert b"streamed " in raw and b"reply" in raw and b'"error"' not in raw
    assert raw.rstrip().endswith(b"data: [DONE]")

    reported = custom_model_cost_microdollars(
        input_tokens=3, output_tokens=20, prompt_price=2_000_000, completion_price=3_000_000
    )
    hold = custom_model_cost_microdollars(
        input_tokens=estimate_tokens_from_messages([{"role": "user", "content": "hello"}]),
        output_tokens=100,
        prompt_price=2_000_000,
        completion_price=3_000_000,
    )
    charged = min(reported, hold)
    assert payer_money.total_usage_microdollars - usage_before == charged
    assert payer_money.reserved_microdollars == 0
    assert STORE.earnings_summary(created["owner_user_id"])["total_earned"] == (
        owner_share_microdollars(charged)
    )
    stored = STORE.get_user_model(created["id"])
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 0
    generation = next(iter(STORE.target.generation_store.generations.values()))
    assert generation.custom_model_id == created["id"]
    assert generation.tokens_completion == 20


def _clock_signature(secret: str, body: bytes = b"") -> str:
    import hashlib
    import hmac
    import time

    timestamp = int(time.time())
    digest = hmac.new(
        secret.encode("utf-8"),
        str(timestamp).encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_clock_calls_accept_the_models_own_signing_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A laptop that clocks one model in should not hold a key that can mint
    API keys or spend the workspace's credits."""

    async def passing_probe(*_args: Any, **_kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=True, detail="ok")

    monkeypatch.setattr("trusted_router.routes.user_models.probe_user_model", passing_probe)
    created = _create(client)
    secret = created["signing_secret"]
    signed = {"TR-Signature": _clock_signature(secret)}

    clocked_in = client.post(f"/v1/user-models/{created['id']}/clock-in", headers=signed)
    assert clocked_in.status_code == 200, clocked_in.text
    assert clocked_in.json()["data"]["online"] is True

    beat = client.post(f"/v1/user-models/{created['id']}/heartbeat", headers=signed)
    assert beat.status_code == 200, beat.text
    assert beat.json()["data"]["heartbeat_expires_at"]

    clocked_out = client.post(f"/v1/user-models/{created['id']}/clock-out", headers=signed)
    assert clocked_out.status_code == 200, clocked_out.text
    assert clocked_out.json()["data"]["online"] is False


def test_clock_signature_is_rejected_when_wrong_stale_or_for_another_model(
    client: TestClient,
) -> None:
    import hashlib
    import hmac

    created = _create(client)
    other = _create(client, body=_body(slug="second-signed-model"))
    secret = created["signing_secret"]
    model = created["id"]

    wrong = client.post(
        f"/v1/user-models/{model}/heartbeat",
        headers={"TR-Signature": _clock_signature("not-the-secret")},
    )
    assert wrong.status_code == 401

    stale_timestamp = 1_700_000_000
    stale_digest = hmac.new(
        secret.encode("utf-8"),
        str(stale_timestamp).encode("ascii") + b".",
        hashlib.sha256,
    ).hexdigest()
    stale = client.post(
        f"/v1/user-models/{model}/heartbeat",
        headers={"TR-Signature": f"t={stale_timestamp},v1={stale_digest}"},
    )
    assert stale.status_code == 401

    # A signature valid for one model must not clock in a different one.
    crossed = client.post(
        f"/v1/user-models/{other['id']}/heartbeat",
        headers={"TR-Signature": _clock_signature(secret)},
    )
    assert crossed.status_code == 401

    malformed = client.post(
        f"/v1/user-models/{model}/heartbeat",
        headers={"TR-Signature": "garbage"},
    )
    assert malformed.status_code == 401

    # No credential at all is still a management-auth failure, not a 401 bypass.
    none = client.post(f"/v1/user-models/{model}/heartbeat")
    assert none.status_code in {401, 403}


def test_editing_a_model_still_requires_the_owner_account(client: TestClient) -> None:
    """The signing secret buys availability, not control: whoever holds it must
    not be able to re-point endpoint_url and take the traffic."""
    created = _create(client)
    response = client.patch(
        f"/v1/user-models/{created['id']}",
        headers={"TR-Signature": _clock_signature(created["signing_secret"])},
        json={"endpoint_url": "https://attacker.example/v1"},
    )
    assert response.status_code in {401, 403}


def test_console_form_states_the_price_bounds_and_explains_the_id_fields(
    client: TestClient,
) -> None:
    """A price the API will reject must not be submittable, and the three
    id-ish fields have to say which is which — 'price is outside the allowed
    range' after a round trip is the worst possible way to learn the rule."""
    user = STORE.ensure_user("console-help@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="user model console help",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)

    page = client.get("/console/user-models")
    assert page.status_code == 200
    # the caps the API enforces, available to the form before submit
    assert str(HUMAN_PRICE_MAX_MICRODOLLARS_PER_M) in page.text
    assert str(MACHINE_PRICE_MAX_MICRODOLLARS_PER_M) in page.text
    assert 'id="price-bounds"' in page.text
    assert "data-price-echo" in page.text
    # slug vs handle vs upstream model id
    assert "The last part of the id callers type" in page.text
    assert "Who callers see as the operator" in page.text
    assert "Callers never see it" in page.text


def test_a_human_model_can_be_registered_for_free(client: TestClient) -> None:
    """Pricing a human model at zero is the first thing anyone does to test one."""
    body = _body(slug="free-human", kind="human")
    body["prompt_price_microdollars_per_million_tokens"] = 0
    body["completion_price_microdollars_per_million_tokens"] = 0
    created = _create(client, body=body)
    assert created["prompt_price_microdollars_per_million_tokens"] == 0

    over = _body(slug="too-expensive-human", kind="human")
    over["prompt_price_microdollars_per_million_tokens"] = (
        HUMAN_PRICE_MAX_MICRODOLLARS_PER_M + 1
    )
    response = client.post("/v1/user-models", headers=HEADERS, json=over)
    assert response.status_code == 400
    # the message says the rule, not just that a rule exists
    assert str(HUMAN_PRICE_MAX_MICRODOLLARS_PER_M) in response.json()["error"]["message"]
