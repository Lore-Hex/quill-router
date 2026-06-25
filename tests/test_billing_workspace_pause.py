"""Workspace billing-pause = the enforced QUIESCE primitive for the typed-billing
migration. When paused, the gateway rejects NEW authorize/validate and key
creation (so in-flight holds can drain to zero before a flip); settle of
already-authorized requests is unaffected (it routes by reservation origin).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, Workspace


def _client_and_key(email: str) -> tuple[TestClient, dict]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k"}
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def _authorize(client: TestClient, key_hash: str):
    return client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 8_000,
            "max_output_tokens": 1_000,
        },
    )


def test_paused_workspace_rejects_authorize_and_validate() -> None:
    client, key = _client_and_key("pause-authz@example.com")
    ws_id = STORE.get_key_by_hash(key["hash"]).workspace_id

    assert _authorize(client, key["hash"]).status_code == 200  # baseline: not paused

    STORE.update_workspace(ws_id, billing_paused=True, billing_pause_reason="flip")
    paused = _authorize(client, key["hash"])
    assert paused.status_code == 503
    assert paused.headers.get("retry-after") == "30"  # tell SDKs to back off, not hammer

    validate = client.post(
        "/v1/internal/gateway/validate",
        json={"api_key_hash": key["hash"], "route_type": "chat"},
    )
    assert validate.status_code == 503, validate.text

    STORE.update_workspace(ws_id, billing_paused=False)
    assert _authorize(client, key["hash"]).status_code == 200  # unpause restores


def test_settle_is_not_blocked_while_paused() -> None:
    """In-flight requests must still settle while paused (settle routes by
    reservation origin, not through the paused endpoints) — else holds strand."""
    client, key = _client_and_key("pause-settle@example.com")
    ws_id = STORE.get_key_by_hash(key["hash"]).workspace_id
    auth = _authorize(client, key["hash"])
    assert auth.status_code == 200
    authorization_id = auth.json()["data"]["authorization_id"]

    STORE.update_workspace(ws_id, billing_paused=True)
    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": 10,
            "actual_output_tokens": 5,
            "request_id": "pause-settle-req",
            "elapsed_seconds": 1.0,
        },
    )
    assert settle.status_code == 200, settle.text  # NOT 503


def test_assert_workspace_billing_active_helper() -> None:
    from fastapi import HTTPException

    from trusted_router.errors import assert_workspace_billing_active

    assert assert_workspace_billing_active(None) is None  # no workspace → no-op
    active = Workspace(id="w", name="n", owner_user_id="u")
    assert assert_workspace_billing_active(active) is None  # not paused → no-op
    paused = Workspace(id="w", name="n", owner_user_id="u", billing_paused=True)
    try:
        assert_workspace_billing_active(paused)
        raise AssertionError("expected a 503")
    except HTTPException as exc:
        assert exc.status_code == 503
        assert exc.headers["Retry-After"] == "30"


def test_paused_workspace_blocks_key_creation() -> None:
    email = "pause-keys@example.com"
    client, key = _client_and_key(email)
    ws_id = STORE.get_key_by_hash(key["hash"]).workspace_id

    STORE.update_workspace(ws_id, billing_paused=True)
    blocked = client.post("/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k2"})
    assert blocked.status_code == 503, blocked.text

    STORE.update_workspace(ws_id, billing_paused=False)
    ok = client.post("/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k3"})
    assert ok.status_code == 201, ok.text


def test_billing_paused_defaults_false_back_compat() -> None:
    # Old workspace rows / objects without the field default to not-paused.
    ws = Workspace(id="w", name="n", owner_user_id="u")
    assert ws.billing_paused is False
    assert ws.billing_pause_reason == ""
