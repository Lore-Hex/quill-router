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
from trusted_router.storage import STORE, ApiKey, Generation, OAuthApp
from trusted_router.types import UsageType
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


def _generation(*, generation_id: str, workspace_id: str, key_hash: str) -> Generation:
    return Generation(
        id=generation_id,
        request_id=f"request-{generation_id}",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model="anthropic/claude-haiku-4.5",
        provider_name="Anthropic",
        app="test",
        tokens_prompt=1,
        tokens_completion=1,
        total_cost_microdollars=1,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=1.0,
        finish_reason="stop",
        status="completed",
        streamed=False,
    )


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


def test_legacy_ownerless_userinfo_preserves_origin_main_shape(
    client: TestClient,
) -> None:
    raw_key, key = _make_key(
        scopes=[],
        creator=False,
        email="legacy-ownerless@example.com",
    )

    response = client.get("/auth/userinfo", headers=_headers(raw_key))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "data": {
            "sub": None,
            "workspace_id": key.workspace_id,
        }
    }


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


def test_scoped_inference_key_passes_all_gateway_sites_including_media(
    client: TestClient,
) -> None:
    _raw, key = _make_key(scopes=[SCOPE_INFERENCE], email="gateway-positive@example.com")
    user = STORE.get_user(key.creator_user_id or "")
    assert user is not None
    custom_model = STORE.create_custom_model(
        owner_user_id=user.id,
        owner_workspace_id=key.workspace_id,
        name="Gateway scope wrapper",
        base_model_id="anthropic/claude-haiku-4.5",
        hidden_prompt="private",
    )
    lookup = {"api_key_lookup_hash": key.lookup_hash}

    validate = client.post(
        "/v1/internal/gateway/validate",
        json={**lookup, "route_type": "images"},
    )
    key_info = client.post("/v1/internal/gateway/key", json=lookup)
    resolved = client.post(
        "/v1/internal/gateway/resolve-custom-model",
        json={**lookup, "model": custom_model.id, "route_type": "chat.completions"},
    )
    authorized = client.post(
        "/v1/internal/gateway/authorize",
        json={
            **lookup,
            "model": "google/gemini-3.1-flash-image",
            "estimated_input_tokens": 16,
            "max_output_tokens": 1120,
            "route_type": "images",
            "idempotency_key": "scoped-image-authorize",
            "request_fingerprint": "a" * 64,
        },
    )

    assert validate.status_code == 200, validate.text
    assert validate.json()["data"] == {
        "workspace_id": key.workspace_id,
        "api_key_hash": key.hash,
        "route_type": "images",
    }
    assert key_info.status_code == 200, key_info.text
    assert key_info.json()["data"]["hash"] == key.hash
    assert key_info.json()["data"]["scopes"] == [SCOPE_INFERENCE]
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["data"]["custom_model"]["id"] == custom_model.id
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["data"]["authorization_id"]
    assert authorized.json()["data"]["model"] == "google/gemini-3.1-flash-image"


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
        assert reachable.status_code == 402, reachable.text
        assert reachable.json()["error"] == {
            "code": 402,
            "message": "Stablecoin payment required to add TrustedRouter credits",
            "type": "insufficient_credits",
        }
        challenge = reachable.json()["data"]
        assert challenge["payment_protocol"] == "x402"
        assert challenge["provider"] == "stripe"
        assert challenge["payment_required"]["x402Version"] == 2
        assert challenge["payment_required"]["error"] == "payment_required"
        assert reachable.headers["payment-required"] == challenge["payment_required_header"]


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


def test_scoped_generation_reads_are_key_isolated_over_http_and_mcp(
    client: TestClient,
) -> None:
    user = STORE.ensure_user("generation-scopes@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    scoped_a_raw, scoped_a = STORE.create_api_key(
        workspace_id=workspace.id,
        name="scoped a",
        creator_user_id=user.id,
        scopes=[SCOPE_INFERENCE],
    )
    _scoped_b_raw, scoped_b = STORE.create_api_key(
        workspace_id=workspace.id,
        name="scoped b",
        creator_user_id=user.id,
        scopes=[SCOPE_INFERENCE],
    )
    legacy_raw, _legacy = STORE.create_api_key(
        workspace_id=workspace.id,
        name="legacy",
        creator_user_id=user.id,
        scopes=[],
    )
    STORE.add_generation(
        _generation(
            generation_id="gen-scoped-a",
            workspace_id=workspace.id,
            key_hash=scoped_a.hash,
        )
    )
    STORE.add_generation(
        _generation(
            generation_id="gen-scoped-b",
            workspace_id=workspace.id,
            key_hash=scoped_b.hash,
        )
    )

    other_http = client.get(
        "/v1/generation",
        headers=_headers(scoped_a_raw),
        params={"id": "gen-scoped-b"},
    )
    own_http = client.get(
        "/v1/generation",
        headers=_headers(scoped_a_raw),
        params={"id": "gen-scoped-a"},
    )
    legacy_http = client.get(
        "/v1/generation",
        headers=_headers(legacy_raw),
        params={"id": "gen-scoped-b"},
    )
    other_mcp = _mcp_call(
        client,
        scoped_a_raw,
        "generation-get",
        {"id": "gen-scoped-b"},
    )

    assert other_http.status_code == 404
    assert _error(other_http)["type"] == "not_found"
    assert own_http.status_code == 200, own_http.text
    assert own_http.json()["data"]["id"] == "gen-scoped-a"
    assert legacy_http.status_code == 200, legacy_http.text
    assert legacy_http.json()["data"]["id"] == "gen-scoped-b"
    assert other_mcp["result"]["isError"] is True
    assert "Unknown generation: gen-scoped-b" in other_mcp["result"]["content"][0]["text"]


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
    assert [_error(response)["type"] for response in responses] == [
        "forbidden",
        "forbidden",
        "forbidden",
        "forbidden",
    ]


def test_scoped_and_legacy_self_key_shapes(client: TestClient) -> None:
    user = STORE.ensure_user("key-shapes@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]

    def create(scopes: list[str], name: str) -> tuple[str, ApiKey]:
        return STORE.create_api_key(
            workspace_id=workspace.id,
            name=name,
            creator_user_id=user.id,
            scopes=scopes,
            limit_microdollars=5_000_000,
            limit_daily_microdollars=1_000_000,
            limit_weekly_microdollars=2_000_000,
            limit_monthly_microdollars=3_000_000,
        )

    inference_raw, inference_key = create([SCOPE_INFERENCE], "inference")
    profile_raw, profile_key = create([SCOPE_INFERENCE, SCOPE_PROFILE], "profile")
    legacy_raw, legacy_key = create([], "legacy")

    inference = client.get("/v1/key", headers=_headers(inference_raw))
    profile = client.get("/v1/key", headers=_headers(profile_raw))
    legacy = client.get("/v1/key", headers=_headers(legacy_raw))
    gateway = client.post(
        "/v1/internal/gateway/key",
        json={"api_key_lookup_hash": inference_key.lookup_hash},
    )

    assert inference.status_code == 200, inference.text
    inference_data = inference.json()["data"]
    assert "creator_user_id" not in inference_data
    assert "management" not in inference_data
    assert "workspace_id" not in inference_data
    assert "tags" not in inference_data
    assert inference_data["scopes"] == [SCOPE_INFERENCE]
    assert inference_data["limit_microdollars"] == 5_000_000
    assert "limit_remaining_microdollars" in inference_data
    assert "usage_microdollars" in inference_data
    assert "reserved_microdollars" in inference_data
    assert gateway.status_code == 200, gateway.text
    assert gateway.json()["data"] == inference_data

    assert profile.status_code == 200, profile.text
    assert profile.json()["data"]["creator_user_id"] == profile_key.creator_user_id
    assert "management" not in profile.json()["data"]

    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["data"]["hash"] == legacy_key.hash
    assert set(legacy.json()["data"]) == {
        "hash",
        "name",
        "label",
        "disabled",
        "limit",
        "limit_microdollars",
        "limit_remaining",
        "limit_remaining_microdollars",
        "limit_reset",
        "include_byok_in_limit",
        "tags",
        "budget_alert_only",
        "usage",
        "usage_microdollars",
        "usage_daily",
        "usage_daily_microdollars",
        "usage_weekly",
        "usage_weekly_microdollars",
        "usage_monthly",
        "usage_monthly_microdollars",
        "byok_usage",
        "byok_usage_microdollars",
        "byok_usage_daily",
        "byok_usage_daily_microdollars",
        "byok_usage_weekly",
        "byok_usage_weekly_microdollars",
        "byok_usage_monthly",
        "byok_usage_monthly_microdollars",
        "limit_daily",
        "limit_daily_microdollars",
        "limit_daily_remaining",
        "limit_daily_remaining_microdollars",
        "limit_daily_resets_at",
        "limit_weekly",
        "limit_weekly_microdollars",
        "limit_weekly_remaining",
        "limit_weekly_remaining_microdollars",
        "limit_weekly_resets_at",
        "limit_monthly",
        "limit_monthly_microdollars",
        "limit_monthly_remaining",
        "limit_monthly_remaining_microdollars",
        "limit_monthly_resets_at",
        "reserved_microdollars",
        "created_at",
        "updated_at",
        "expires_at",
        "creator_user_id",
        "workspace_id",
        "management",
    }


def test_credits_summary_uses_live_usage_reserved_and_zero_floor(
    client: TestClient,
) -> None:
    raw_key, key = _make_key(scopes=[SCOPE_BALANCE_READ], email="summary-live@example.com")
    settled = STORE.reserve(key.workspace_id, key.hash, 2_000_000)
    STORE.settle(settled.id, 3_000_000)
    open_reservation = STORE.reserve(key.workspace_id, key.hash, 1_000_000)

    nonzero = client.get("/v1/credits/summary", headers=_headers(raw_key))

    assert nonzero.status_code == 200, nonzero.text
    assert nonzero.json() == {
        "data": {
            "remaining_microdollars": 6_000_000,
            "remaining": "6",
        }
    }

    STORE.settle(open_reservation.id, 10_000_001)
    overdrawn = client.get("/v1/credits/summary", headers=_headers(raw_key))
    assert overdrawn.status_code == 200, overdrawn.text
    assert overdrawn.json() == {
        "data": {
            "remaining_microdollars": 0,
            "remaining": "0",
        }
    }


def test_wallet_only_identity_matches_userinfo_and_exchange(client: TestClient) -> None:
    user = STORE.create_wallet_user("0x1111111111111111111111111111111111111111")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="metamask",
        label=user.wallet_address or "wallet",
        workspace_id=workspace.id,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)
    code_response = client.post(
        "/v1/auth/keys/code",
        json={"callback_url": "https://wallet-app.example.com/callback"},
    )
    assert code_response.status_code == 200, code_response.text

    exchange = client.post(
        "/v1/auth/keys",
        json={"code": code_response.json()["data"]["id"]},
    )
    assert exchange.status_code == 200, exchange.text
    userinfo = client.get(
        "/v1/auth/userinfo",
        headers=_headers(exchange.json()["key"]),
    )

    expected = {
        "sub": user.id,
        "email": None,
        "email_verified": False,
        "phone_verified": False,
        "identity_verified": False,
        "verification_level": "none",
        "wallet_address": user.wallet_address,
        "workspace_id": workspace.id,
        "created_at": user.created_at,
    }
    assert exchange.json()["identity"] == expected
    assert userinfo.status_code == 200, userinfo.text
    assert userinfo.json() == {"data": expected}


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
        STORE.create_oauth_app(
            OAuthApp(
                id="federated-app",
                owner_user_id=user.id,
                name="Federated App",
                redirect_uris=["https://federated.example/callback"],
            )
        )
        raw, key = STORE.create_api_key(
            workspace_id=workspace.id,
            name="fed scoped",
            creator_user_id=user.id,
            scopes=list(DEFAULT_DELEGATED_SCOPES),
            app_id="federated-app",
        )
        del raw
        served = fed_client.post(
            "/internal/federation/resolve-key",
            headers={
                "x-trustedrouter-federation-token": "peer-secret",
                "x-trustedrouter-federation-features": "scopes",
            },
            json={"api_key_lookup_hash": key.lookup_hash},
        )
        assert served.status_code == 200, served.text
        record = served.json()["data"]
        assert record["scopes"] == list(DEFAULT_DELEGATED_SCOPES)
        assert record["app_id"] == "federated-app"
        assert record["app_suspended"] is False
        imported = federated_api_key_from_record(record)
        assert list(imported.scopes) == list(DEFAULT_DELEGATED_SCOPES)
        assert imported.app_id == "federated-app"
        assert imported.federated_app_suspended is False


def test_federation_feature_declaration_fails_closed_only_for_scoped_keys() -> None:
    app = create_app(
        Settings(
            environment="test",
            federation_peer_token="peer-secret",  # noqa: S106 - test fixture.
        ),
        init_observability=False,
    )
    with TestClient(app) as fed_client:
        user = STORE.ensure_user("fed-capability@example.com")
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        _scoped_raw, scoped = STORE.create_api_key(
            workspace_id=workspace.id,
            name="scoped",
            creator_user_id=user.id,
            scopes=[SCOPE_INFERENCE],
        )
        _legacy_raw, legacy = STORE.create_api_key(
            workspace_id=workspace.id,
            name="legacy",
            creator_user_id=user.id,
            scopes=[],
        )
        headers = {"x-trustedrouter-federation-token": "peer-secret"}

        unknown = fed_client.post(
            "/internal/federation/resolve-key",
            headers=headers,
            json={"api_key_lookup_hash": "not-a-key"},
        )
        undeclared_scoped = fed_client.post(
            "/internal/federation/resolve-key",
            headers=headers,
            json={"api_key_lookup_hash": scoped.lookup_hash},
        )
        served_legacy = fed_client.post(
            "/internal/federation/resolve-key",
            headers=headers,
            json={"api_key_lookup_hash": legacy.lookup_hash},
        )

        assert undeclared_scoped.status_code == 404
        assert undeclared_scoped.json() == unknown.json()
        assert served_legacy.status_code == 200, served_legacy.text
        assert served_legacy.json()["data"]["scopes"] == []
        assert "app_id" not in served_legacy.json()["data"]
        assert "app_suspended" not in served_legacy.json()["data"]
