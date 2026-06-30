from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_types import estimate_tokens_from_text
from trusted_router.storage import STORE


def _create_key(client: TestClient, email: str = "alice@example.com") -> dict:
    resp = client.post("/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k"})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _create_custom_model(
    client: TestClient,
    *,
    email: str = "alice@example.com",
    name: str = "Private model",
    base_model_id: str = "anthropic/claude-sonnet-4.6",
    hidden_prompt: str = "private policy",
    enabled: bool = True,
    slug: str | None = None,
) -> dict:
    body = {
        "name": name,
        "base_model_id": base_model_id,
        "hidden_prompt": hidden_prompt,
        "enabled": enabled,
    }
    if slug is not None:
        body["slug"] = slug
    resp = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": email},
        json=body,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_custom_model_crud_owner_limit_and_public_catalog_redaction(client: TestClient) -> None:
    created = _create_custom_model(client, hidden_prompt="secret reviewer prompt")
    assert created["id"].startswith("trustedrouter/user-")
    assert created["hidden_prompt"] == "secret reviewer prompt"
    assert created["revision"] == 1

    listed = client.get("/v1/custom-models", headers={"x-trustedrouter-user": "alice@example.com"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["id"] == created["id"]
    assert listed.json()["data"][0]["hidden_prompt"] == "secret reviewer prompt"

    public_models = client.get("/v1/models")
    assert public_models.status_code == 200, public_models.text
    assert created["id"] not in public_models.text
    assert "secret reviewer prompt" not in public_models.text

    denied = client.get(
        f"/v1/custom-models/{created['id']}",
        headers={"x-trustedrouter-user": "bob@example.com"},
    )
    assert denied.status_code == 404

    patched = client.patch(
        f"/v1/custom-models/{created['id']}",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "Updated", "hidden_prompt": "new secret", "enabled": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["name"] == "Updated"
    assert patched.json()["data"]["hidden_prompt"] == "new secret"
    assert patched.json()["data"]["revision"] == 2
    assert patched.json()["data"]["enabled"] is False

    deleted = client.delete(
        f"/v1/custom-models/{created['id']}",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"] == {"deleted": True, "id": created["id"]}

    ids = {
        _create_custom_model(client, name=f"m{i}", hidden_prompt=f"prompt {i}")["id"]
        for i in range(10)
    }
    assert len(ids) == 10
    over_limit = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "too many",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "x",
        },
    )
    assert over_limit.status_code == 400


def test_custom_model_slug_create_duplicate_and_rename(client: TestClient) -> None:
    created = _create_custom_model(client, slug="legal-reviewer")
    assert created["id"] == "trustedrouter/user-legal-reviewer"
    assert created["slug"] == "legal-reviewer"

    duplicate = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "dupe",
            "slug": "trustedrouter/user-legal-reviewer",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "x",
        },
    )
    assert duplicate.status_code == 409

    invalid = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "bad slug",
            "slug": "not_ok",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "x",
        },
    )
    assert invalid.status_code == 400

    renamed = client.patch(
        f"/v1/custom-models/{created['id']}",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"slug": "litigation-briefs"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["id"] == "trustedrouter/user-litigation-briefs"
    assert renamed.json()["data"]["slug"] == "litigation-briefs"
    assert renamed.json()["data"]["revision"] == 2

    old = client.get(
        f"/v1/custom-models/{created['id']}",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    assert old.status_code == 404
    new = client.get(
        "/v1/custom-models/trustedrouter/user-litigation-briefs",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    assert new.status_code == 200


def test_custom_models_validate_prompt_length_and_base_model(client: TestClient) -> None:
    too_long = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "too long",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "x" * 262_145,
        },
    )
    assert too_long.status_code == 400

    nested = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "nested",
            "base_model_id": "trustedrouter/user-abc123",
            "hidden_prompt": "x",
        },
    )
    assert nested.status_code == 400

    invalid = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "bad", "base_model_id": "missing/model", "hidden_prompt": "x"},
    )
    assert invalid.status_code == 400

    orchestration = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "orchestration",
            "base_model_id": "trustedrouter/socrates-1.0",
            "hidden_prompt": "x",
        },
    )
    assert orchestration.status_code == 201, orchestration.text

    synth = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "custom prometheus",
            "base_model_id": "trustedrouter/prometheus-1.0",
            "hidden_prompt": "x",
        },
    )
    assert synth.status_code == 201, synth.text

    routing_alias = client.post(
        "/v1/custom-models",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "name": "zdr custom",
            "base_model_id": "trustedrouter/zdr",
            "hidden_prompt": "x",
        },
    )
    assert routing_alias.status_code == 201, routing_alias.text


def test_gateway_authorizes_custom_model_against_base_model_and_revision(
    client: TestClient,
) -> None:
    key = _create_key(client)
    custom = _create_custom_model(
        client,
        base_model_id="anthropic/claude-sonnet-4.6",
        hidden_prompt="hidden billing prompt",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        headers={"idempotency-key": "custom-idem"},
        json={
            "api_key_hash": key["hash"],
            "model": custom["id"],
            "estimated_input_tokens": 100,
            "max_output_tokens": 10,
        },
    )
    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["requested_model"] == custom["id"]
    assert data["model"] == "anthropic/claude-sonnet-4.6"
    assert data["custom_model"] == {
        "id": custom["id"],
        "name": "Private model",
        "base_model_id": "anthropic/claude-sonnet-4.6",
        "hidden_prompt": "hidden billing prompt",
        "revision": 1,
    }

    authorization = STORE.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None
    assert authorization.custom_model_id == custom["id"]
    assert authorization.custom_model_revision == 1
    assert authorization.model_id == "anthropic/claude-sonnet-4.6"
    assert authorization.estimated_microdollars > 0
    # The reserve estimate should include the hidden preamble because the
    # enclave prepends it before the upstream provider call.
    hidden_tokens = estimate_tokens_from_text("hidden billing prompt")
    baseline = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "anthropic/claude-sonnet-4.6",
            "estimated_input_tokens": 100,
            "max_output_tokens": 10,
        },
    )
    assert baseline.status_code == 200, baseline.text
    baseline_authorization = STORE.get_gateway_authorization(
        baseline.json()["data"]["authorization_id"]
    )
    assert baseline_authorization is not None
    assert authorization.estimated_microdollars >= baseline_authorization.estimated_microdollars
    assert hidden_tokens >= 1

    client.patch(
        f"/v1/custom-models/{custom['id']}",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"hidden_prompt": "changed prompt"},
    )
    replay = client.post(
        "/v1/internal/gateway/authorize",
        headers={"idempotency-key": "custom-idem"},
        json={
            "api_key_hash": key["hash"],
            "model": custom["id"],
            "estimated_input_tokens": 100,
            "max_output_tokens": 10,
        },
    )
    assert replay.status_code == 409


def test_gateway_authorizes_custom_model_backed_by_orchestration_alias(
    client: TestClient,
) -> None:
    key = _create_key(client)
    custom = _create_custom_model(
        client,
        name="Private Socrates",
        base_model_id="trustedrouter/socrates-1.0",
        hidden_prompt="use the private playbook",
    )

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": custom["id"],
            "estimated_input_tokens": 100,
            "max_output_tokens": 10,
        },
    )
    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["requested_model"] == custom["id"]
    assert data["model"] == "cerebras/gpt-oss-120b"
    assert data["custom_model"]["base_model_id"] == "trustedrouter/socrates-1.0"
    assert data["route_candidates"], data


def test_gateway_resolves_custom_model_without_authorizing_outer_hold(
    client: TestClient,
) -> None:
    key = _create_key(client)
    custom = _create_custom_model(
        client,
        name="Private Prometheus",
        base_model_id="trustedrouter/prometheus-1.0",
        hidden_prompt="private synthesis policy",
    )

    resolved = client.post(
        "/v1/internal/gateway/resolve-custom-model",
        json={
            "api_key_hash": key["hash"],
            "model": custom["id"],
            "route_type": "chat.completions",
        },
    )
    assert resolved.status_code == 200, resolved.text
    data = resolved.json()["data"]
    assert data["workspace_id"] == key["workspace_id"]
    assert data["api_key_hash"] == key["hash"]
    assert data["route_type"] == "chat.completions"
    assert data["custom_model"] == {
        "id": custom["id"],
        "name": "Private Prometheus",
        "base_model_id": "trustedrouter/prometheus-1.0",
        "hidden_prompt": "private synthesis policy",
        "revision": 1,
    }
    assert STORE.get_gateway_authorization_by_idempotency_key(
        key["workspace_id"], key["hash"], "unused"
    ) is None


def test_gateway_rejects_custom_models_in_fallback_arrays_and_disabled_aliases(
    client: TestClient,
) -> None:
    key = _create_key(client)
    custom = _create_custom_model(client, enabled=False)

    disabled = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": custom["id"],
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        },
    )
    assert disabled.status_code == 404

    in_fallback = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "anthropic/claude-sonnet-4.6",
            "models": [custom["id"]],
            "estimated_input_tokens": 10,
            "max_output_tokens": 10,
        },
    )
    assert in_fallback.status_code == 400


def test_console_custom_models_and_user_chat_locked_model_smoke() -> None:
    client = TestClient(create_app(Settings(environment="local"), init_observability=False))
    user = STORE.ensure_user("alice@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)

    page = client.get("/console/custom-models")
    assert page.status_code == 200
    assert "Custom Models" in page.text
    assert "Create custom model" in page.text
    assert "/static/model_catalog.js" in page.text
    assert "data-base-model-picker" in page.text
    assert "Socrates, Prometheus, Zeus" in page.text
    assert "Published" in page.text
    assert "Enabled" not in page.text

    created = client.post(
        "/console/custom-models",
        data={
            "name": "Console model",
            "slug": "console-model",
            "base_model_id": "anthropic/claude-sonnet-4.6",
            "hidden_prompt": "console secret",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    custom = STORE.list_custom_models_for_user(user.id)[0]

    updated_page = client.get("/console/custom-models")
    assert updated_page.status_code == 200
    assert custom.id in updated_page.text
    assert "console-model" in updated_page.text
    assert f"/user-chat?model={custom.id}" in updated_page.text

    chat = client.get(f"/user-chat?model={custom.id}")
    assert chat.status_code == 200
    assert custom.id in chat.text
    assert "tr_user_chat_state_trustedrouter_user_" in chat.text
