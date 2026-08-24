"""POST /internal/gateway/key — the enclave's /v1/key passthrough backend.

Keyed by api_key_lookup_hash + the internal gateway token so the raw bearer
NEVER leaves the enclave (the attested contract; authorize does the same).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def _make_key(**kwargs):
    user = STORE.ensure_user("keyinfo@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    raw, key = STORE.create_api_key(
        workspace_id=ws.id, name="k", creator_user_id=user.id, **kwargs
    )
    return raw, key


def test_key_info_by_lookup_hash_returns_budget_fields(client: TestClient) -> None:
    _raw, key = _make_key(limit_daily_microdollars=2_000_000)
    resp = client.post(
        "/v1/internal/gateway/key",
        json={"api_key_lookup_hash": key.lookup_hash},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["hash"] == key.hash
    assert data["limit_daily"] == 2.0
    assert data["limit_daily_resets_at"].endswith("Z")
    assert "usage_daily_microdollars" in data


def test_key_info_rejects_disabled_and_unknown(client: TestClient) -> None:
    _raw, key = _make_key()
    STORE.update_key(key.hash, {"disabled": True})
    resp = client.post(
        "/v1/internal/gateway/key",
        json={"api_key_lookup_hash": key.lookup_hash},
    )
    assert resp.status_code == 401

    resp = client.post(
        "/v1/internal/gateway/key",
        json={"api_key_lookup_hash": "sha256-of-nothing"},
    )
    assert resp.status_code == 401


def test_key_info_allowed_while_workspace_paused(client: TestClient) -> None:
    """Reading your own budget while billing-paused is a harmless read — the
    pause gate deliberately does not apply here."""
    _raw, key = _make_key()
    STORE.update_workspace(key.workspace_id, billing_paused=True)
    try:
        resp = client.post(
            "/v1/internal/gateway/key",
            json={"api_key_lookup_hash": key.lookup_hash},
        )
        assert resp.status_code == 200, resp.text
    finally:
        STORE.update_workspace(key.workspace_id, billing_paused=False)


def test_key_info_requires_internal_token_when_configured() -> None:
    """codex #95: prove the internal gateway token is enforced (the default
    test fixture leaves it None, so the other tests don't exercise it)."""
    from fastapi.testclient import TestClient

    from trusted_router.config import Settings
    from trusted_router.main import create_app

    internal_token = "internal" + "-keyinfo-token"
    app = create_app(Settings(environment="test", internal_gateway_token=internal_token))
    tc = TestClient(app)
    user = STORE.ensure_user("keyinfo-tok@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(workspace_id=ws.id, name="k", creator_user_id=user.id)
    body = {"api_key_lookup_hash": key.lookup_hash}

    missing = tc.post("/v1/internal/gateway/key", json=body)
    wrong = tc.post(
        "/v1/internal/gateway/key",
        headers={"x-trustedrouter-internal-token": "wrong"},
        json=body,
    )
    correct = tc.post(
        "/v1/internal/gateway/key",
        headers={"x-trustedrouter-internal-token": internal_token},
        json=body,
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert correct.status_code == 200, correct.text
