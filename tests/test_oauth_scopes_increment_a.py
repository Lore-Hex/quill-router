from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal.gateway import _assert_gateway_key_scope
from trusted_router.routes.mcp import MCPToolError, TrustedRouterMCP
from trusted_router.scopes import (
    DEFAULT_DELEGATED_SCOPES,
    SCOPE_BALANCE_READ,
    SCOPE_INFERENCE,
    SCOPE_PROFILE,
)
from trusted_router.storage import STORE, ApiKey
from trusted_router.verification import verification_level


def _make_key(
    *,
    scopes: list[str],
    creator: bool = True,
    management: bool = False,
    email: str = "scoped@example.com",
) -> tuple[str, ApiKey]:
    user = STORE.ensure_user(email, email=email)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    return STORE.create_api_key(
        workspace_id=workspace.id,
        name="scoped key",
        creator_user_id=user.id if creator else None,
        management=management,
        scopes=scopes,
    )


def _headers(raw_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {raw_key}"}


def _error(response: Any) -> dict[str, Any]:
    return response.json()["error"]


def _mcp_call(
    client: TestClient,
    raw_key: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers=_headers(raw_key),
        json={
            "jsonrpc": "2.0",
            "id": "scope-call",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_verification_level_full_ladder_and_identity_precedence() -> None:
    user = STORE.ensure_user("verification-level@example.com")
    assert verification_level(None) == "none"
    assert verification_level(user) == "none"
    user.email_verified = True
    assert verification_level(user) == "email"
    user.phone_verified = True
    assert verification_level(user) == "phone"
    user.identity_status = "approved"
    assert verification_level(user) == "identity"
    user.email_verified = False
    user.phone_verified = False
    assert verification_level(user) == "identity"


def test_scoped_auth_matrix_and_strict_userinfo(client: TestClient) -> None:
    raw_key, key = _make_key(scopes=DEFAULT_DELEGATED_SCOPES)
    headers = _headers(raw_key)

    assert client.get("/v1/key", headers=headers).status_code == 200
    userinfo = client.get("/auth/userinfo", headers=headers)
    assert userinfo.status_code == 200, userinfo.text
    assert set(userinfo.json()["data"]) == {
        "sub",
        "email",
        "email_verified",
        "phone_verified",
        "identity_verified",
        "verification_level",
        "wallet_address",
        "workspace_id",
        "created_at",
    }
    balance = client.get("/v1/credits/summary", headers=headers)
    assert balance.status_code == 200, balance.text
    assert set(balance.json()["data"]) == {"remaining_microdollars", "remaining"}
    assert client.get("/v1/credits", headers=headers).status_code == 403
    assert client.post("/v1/keys", headers=headers, json={"name": "escalate"}).status_code == 403

    key.creator_user_id = None
    strict = client.get("/auth/userinfo", headers=headers)
    assert strict.status_code == 403
    assert _error(strict)["type"] == "forbidden"


def test_missing_scope_uses_insufficient_scope_not_invalid_api_key(
    client: TestClient,
) -> None:
    inference_raw, _ = _make_key(scopes=[SCOPE_INFERENCE], email="inference-only@example.com")
    response = client.get("/auth/userinfo", headers=_headers(inference_raw))
    assert response.status_code == 403
    assert _error(response)["type"] == "insufficient_scope"
    assert "profile" in _error(response)["message"]

    _profile_raw, profile_key = _make_key(
        scopes=[SCOPE_PROFILE], email="profile-only@example.com"
    )
    with pytest.raises(HTTPException) as exc_info:
        _assert_gateway_key_scope(profile_key)
    assert exc_info.value.status_code == 403
    error = exc_info.value.detail["error"]
    assert error["type"] == "insufficient_scope"
    assert error["type"] != "invalid_api_key"
    _assert_gateway_key_scope(_make_key(scopes=[SCOPE_INFERENCE])[1])


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/internal/gateway/validate", {}),
        ("/v1/internal/gateway/key", {}),
        ("/v1/internal/gateway/resolve-custom-model", {"model": "custom/not-found"}),
        ("/v1/internal/gateway/authorize", {"model": "anthropic/claude-sonnet-5"}),
    ],
)
def test_all_gateway_inline_sites_deny_scoped_key_without_inference(
    client: TestClient,
    path: str,
    body: dict[str, Any],
) -> None:
    _raw, key = _make_key(scopes=[SCOPE_PROFILE], email=f"gateway-{path}@example.com")
    response = client.post(path, json={"api_key_lookup_hash": key.lookup_hash, **body})
    assert response.status_code == 403, response.text
    assert _error(response)["type"] == "insufficient_scope"


def test_x402_denies_delegated_keys_but_legacy_reaches_payment_layer() -> None:
    settings = Settings(
        environment="test",
        x402_enabled=True,
        x402_allow_mock_payments=True,
        stripe_secret_key=None,
        rate_limit_enabled=False,
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        scoped_raw, _ = _make_key(scopes=DEFAULT_DELEGATED_SCOPES, email="x402-scope@example.com")
        for path, body in (
            ("/v1/billing/x402/fund", {"amount": "10.00"}),
            ("/v1/billing/x402/settle", {"payment_intent_id": "pi_scope_denied"}),
        ):
            denied = client.post(path, headers=_headers(scoped_raw), json=body)
            assert denied.status_code == 403
            assert _error(denied)["type"] == "insufficient_scope"

        legacy_raw, _ = _make_key(scopes=[], email="x402-legacy@example.com")
        reachable = client.post(
            "/v1/billing/x402/fund",
            headers=_headers(legacy_raw),
            json={"amount": "10.00"},
        )
        assert reachable.status_code != 403


def test_mcp_scoped_credit_shape_and_per_tool_enforcement(client: TestClient) -> None:
    raw_key, key = _make_key(scopes=DEFAULT_DELEGATED_SCOPES, email="mcp-scope@example.com")
    payload = _mcp_call(client, raw_key, "credits-get")
    result = payload["result"]
    assert result["isError"] is False
    data = json.loads(result["content"][0]["text"])["data"]
    assert set(data) == {"remaining_microdollars", "remaining"}

    server = TrustedRouterMCP(Settings(environment="test"))
    server._require_tool_scope(key, "chat-send")
    profile_key = _make_key(scopes=[SCOPE_PROFILE], email="mcp-profile@example.com")[1]
    with pytest.raises(MCPToolError, match="inference"):
        server._require_tool_scope(profile_key, "chat-send")


def test_scoped_key_cannot_mint_oauth_codes(client: TestClient) -> None:
    raw_key, _ = _make_key(scopes=DEFAULT_DELEGATED_SCOPES, email="oauth-mint@example.com")
    headers = _headers(raw_key)
    callback = "https://app.example.com/callback"
    responses = [
        client.get(f"/auth?callback_url={callback}", headers=headers),
        client.post("/auth/approve", headers=headers, data={"callback_url": callback}),
        client.post(
            "/auth/fund",
            headers=headers,
            data={"callback_url": callback, "fund_amount": "20"},
        ),
        client.post("/auth/keys/code", headers=headers, json={"callback_url": callback}),
    ]
    assert [response.status_code for response in responses] == [403, 403, 403, 403]


def test_key_scope_mint_validation_and_patch_immutability(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    unknown = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "unknown", "scopes": ["unknown"]},
    )
    assert unknown.status_code == 400

    management = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "bad management", "management": True, "scopes": [SCOPE_PROFILE]},
    )
    assert management.status_code == 400

    created = client.post("/v1/keys", headers=user_headers, json={"name": "immutable"})
    assert created.status_code == 201
    for scopes in ([SCOPE_BALANCE_READ], None):
        patched = client.patch(
            f"/v1/keys/{created.json()['data']['hash']}",
            headers=user_headers,
            json={"scopes": scopes},
        )
        assert patched.status_code == 400


def test_federation_resolve_key_serves_scopes_end_to_end() -> None:
    """The peer-plane record must carry scopes: an omission here is
    fail-OPEN (a scoped key arrives at the peer as an unscoped legacy
    key). Round-trips the SERVED dict through the import constructor."""
    from trusted_router.config import Settings
    from trusted_router.main import create_app
    from trusted_router.storage import STORE
    from trusted_router.storage_models import federated_api_key_from_record

    app = create_app(
        Settings(
            environment="test",
            federation_peer_token="peer-secret",  # noqa: S106 - test fixture.
        ),
        init_observability=False,
    )
    with TestClient(app) as fed_client:
        user = STORE.ensure_user("fed-scopes@example.com")
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        raw, key = STORE.create_api_key(
            workspace_id=workspace.id,
            name="fed scoped",
            creator_user_id=user.id,
            scopes=list(DEFAULT_DELEGATED_SCOPES),
        )
        del raw
        served = fed_client.post(
            "/internal/federation/resolve-key",
            headers={"x-trustedrouter-federation-token": "peer-secret"},
            json={"api_key_lookup_hash": key.lookup_hash},
        )
        assert served.status_code == 200, served.text
        record = served.json()["data"]
        assert record["scopes"] == list(DEFAULT_DELEGATED_SCOPES)
        imported = federated_api_key_from_record(record)
        assert list(imported.scopes) == list(DEFAULT_DELEGATED_SCOPES)
