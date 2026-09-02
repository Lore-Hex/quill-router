from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE

HEADERS = {"x-trustedrouter-user": "custom-gate@example.com"}
MODEL_BODY = {
    "name": "Verified model",
    "slug": "verified-model",
    "base_model_id": "anthropic/claude-sonnet-4.6",
    "hidden_prompt": "private policy",
}


def _settings() -> Settings:
    return Settings(environment="test", custom_models_require_verification=True)


def _user(client: TestClient) -> Any:
    client.get("/v1/custom-models", headers=HEADERS)
    user = STORE.find_user_by_email("custom-gate@example.com")
    assert user is not None
    return user


def _verify(user: Any) -> None:
    started = STORE.begin_phone_verification(user.id, "+13059511381", "voice")
    assert started is not None
    code, _updated = started
    assert STORE.confirm_phone_verification(user.id, code)[0] == "ok"
    STORE.set_user_identity_status(
        user.id,
        status="approved",
        verified_name="Verified Creator",
    )
    STORE.claim_user_username(user.id, "verified-creator")


def _direct_model(user: Any) -> Any:
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    return STORE.create_custom_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        owner_username="custom-gate",
        name="Existing",
        slug="existing-model",
        base_model_id="anthropic/claude-sonnet-4.6",
        hidden_prompt="existing",
    )


def test_custom_model_verification_default_is_off_in_test() -> None:
    assert Settings(environment="test").custom_models_verification_enforced is False
    assert Settings(environment="local").custom_models_verification_enforced is False
    assert (
        Settings(
            environment="staging",
            service_surface="control",
            attribution_cookie_secret="staging-attribution-" + "a" * 32,
            stripe_webhook_secret="whsec_" + "staging",
            stripe_secret_key="sk_" + "staging",
        ).custom_models_verification_enforced
        is True
    )


def test_enforced_api_post_and_patch_return_exact_verification_error() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    user = _user(client)
    existing = _direct_model(user)

    created = client.post("/v1/custom-models", headers=HEADERS, json=MODEL_BODY)
    patched = client.patch(
        f"/v1/custom-models/{existing.id}",
        headers=HEADERS,
        json={"name": "Blocked update"},
    )

    for response in (created, patched):
        assert response.status_code == 403
        error = response.json()["error"]
        assert error["type"] == "verification_required"
        assert error["missing_requirements"] == [
            "phone_verified",
            "identity_verified",
            "username",
        ]
        assert error["verification_url"] == "/console/account/verification"


def test_enforcement_does_not_gate_get_list_or_delete() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    user = _user(client)
    existing = _direct_model(user)

    listed = client.get("/v1/custom-models", headers=HEADERS)
    fetched = client.get(f"/v1/custom-models/{existing.id}", headers=HEADERS)
    deleted = client.delete(f"/v1/custom-models/{existing.id}", headers=HEADERS)

    assert listed.status_code == 200
    assert fetched.status_code == 200
    assert deleted.status_code == 200


def test_fully_verified_user_can_create_and_edit_custom_model() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    user = _user(client)
    _verify(user)

    created = client.post("/v1/custom-models", headers=HEADERS, json=MODEL_BODY)
    assert created.status_code == 201
    patched = client.patch(
        f"/v1/custom-models/{created.json()['data']['id']}",
        headers=HEADERS,
        json={"name": "Verified update"},
    )
    assert patched.status_code == 200


def test_api_key_principal_resolves_and_gates_its_creator() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    user = _user(client)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_key, _key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="management",
        creator_user_id=user.id,
        management=True,
    )
    bearer = {"authorization": f"Bearer {raw_key}"}

    blocked = client.post("/v1/custom-models", headers=bearer, json=MODEL_BODY)
    _verify(user)
    allowed = client.post("/v1/custom-models", headers=bearer, json=MODEL_BODY)

    assert blocked.status_code == 403
    assert blocked.json()["error"]["missing_requirements"] == [
        "phone_verified",
        "identity_verified",
        "username",
    ]
    assert allowed.status_code == 201


def test_console_create_and_edit_redirect_to_verification_with_linked_flash() -> None:
    client = TestClient(create_app(_settings(), init_observability=False))
    user = STORE.ensure_user("custom-gate@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="custom gate",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    existing = _direct_model(user)
    form = {
        "name": "Blocked",
        "slug": "blocked-model",
        "base_model_id": "anthropic/claude-sonnet-4.6",
        "hidden_prompt": "blocked",
    }

    created = client.post(
        "/console/custom-models",
        data=form,
        follow_redirects=False,
    )
    edited = client.post(
        f"/console/custom-models/{existing.id}",
        data=form,
        follow_redirects=False,
    )
    flash = client.get("/console/custom-models?error=verification")

    assert created.status_code == 303
    assert edited.status_code == 303
    assert created.headers["location"] == "/console/custom-models?error=verification"
    assert edited.headers["location"] == "/console/custom-models?error=verification"
    assert "Verify your account before creating or editing custom models" in flash.text
    assert 'href="/console/account/verification"' in flash.text
    assert '<fieldset class="form-gate" disabled>' in flash.text
