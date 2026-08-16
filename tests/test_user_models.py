from __future__ import annotations

import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.user_model_dispatch import BufferedUserModelDispatch
from trusted_router.services.user_model_probe import ProbeResult
from trusted_router.services.user_model_secrets import (
    USER_MODEL_ENDPOINT_KEY_PURPOSE,
    USER_MODEL_SECRET_NAMESPACE,
    USER_MODEL_SIGNING_PURPOSE,
)
from trusted_router.storage import STORE

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


def _create_key(client: TestClient) -> dict[str, Any]:
    response = client.post("/v1/keys", headers=HEADERS, json={"name": "gateway"})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_user_model_crud_ownership_and_one_time_secrets(client: TestClient) -> None:
    body = _body()
    body["endpoint_api_key"] = "owner-endpoint-api-key"
    created = _create(client, body=body)
    assert created["id"] == "trustedrouter/user-community-machine"
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


def test_limit_three_and_shared_slug_collisions_both_directions(
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
    collision = client.post(
        "/v1/user-models", headers=HEADERS, json=_body(slug="wrapper-first")
    )
    assert collision.status_code == 409

    _create(client, body=_body(slug="user-first"))
    reverse = client.post(
        "/v1/custom-models",
        headers=HEADERS,
        json={
            "name": "Wrapper",
            "slug": "user-first",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "private",
        },
    )
    assert reverse.status_code == 409

    _create(client, body=_body(slug="second-user-model"))
    _create(client, body=_body(slug="third-user-model"))
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
) -> None:
    client = dispatch_client
    key = _create_key(client)
    created = _create(client)

    response = client.post(
        "/v1/internal/gateway/authorize",
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


def test_https_is_required_outside_local_and_test() -> None:
    settings = Settings(
        environment="staging",
        custom_models_require_verification=False,
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
    model = STORE.get_user_model("trustedrouter/user-console-community")
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
