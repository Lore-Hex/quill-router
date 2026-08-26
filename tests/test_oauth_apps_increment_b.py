from __future__ import annotations

from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.serialization import key_shape
from trusted_router.storage import STORE, InMemoryStore, OAuthApp
from trusted_router.storage_activity import generation_events
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, GatewayAuthorization, Generation
from trusted_router.types import UsageType

CALLBACK_URL = "https://registered.example/callback"
APP_ID = "verified-app"


def _identity_user(
    email: str = "alice@example.com",
    *,
    verified_name: str | None = "Alice Example",
):
    user = STORE.ensure_user(email)
    STORE.set_user_identity_status(
        user.id,
        status="approved",
        verified_name=verified_name,
    )
    return user


def _app_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": APP_ID,
        "name": "Verified App",
        "redirect_uris": [CALLBACK_URL],
        "logo_url": "https://registered.example/logo.png",
        "markup_basis_points": 30_000,
        "suspended": False,
    }
    body.update(overrides)
    return body


def _register_app(
    client: TestClient,
    headers: dict[str, str],
    **overrides: object,
) -> dict[str, object]:
    _identity_user(headers["x-trustedrouter-user"])
    _active_session(client, headers["x-trustedrouter-user"])
    response = client.post("/v1/oauth/apps", headers=headers, json=_app_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _active_session(client: TestClient, email: str = "alice@example.com") -> None:
    user = STORE.ensure_user(email)
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label=email,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)


def test_registration_crud_is_identity_gated_and_owner_scoped(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = _register_app(client, user_headers)
    assert created == {
        "id": APP_ID,
        "owner_user_id": STORE.find_user_by_email("alice@example.com").id,
        "name": "Verified App",
        "redirect_uris": [CALLBACK_URL],
        "logo_url": "https://registered.example/logo.png",
        "markup_basis_points": 30_000,
        "suspended": False,
        "created_at": created["created_at"],
        "updated_at": created["updated_at"],
    }

    listed = client.get("/v1/oauth/apps", headers=user_headers)
    fetched = client.get(f"/v1/oauth/apps/{APP_ID}", headers=user_headers)
    assert listed.status_code == 200
    assert listed.json()["data"] == [created]
    assert fetched.json()["data"] == created

    patched = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={
            "name": "Renamed App",
            "redirect_uris": ["verified-app://callback"],
            "logo_url": None,
            "markup_basis_points": 0,
            "suspended": True,
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"] == {
        **created,
        "name": "Renamed App",
        "redirect_uris": ["verified-app://callback"],
        "logo_url": None,
        "markup_basis_points": 0,
        "suspended": True,
        "updated_at": patched.json()["data"]["updated_at"],
    }

    _active_session(client, "bob@example.com")
    bob_headers = {"x-trustedrouter-user": "bob@example.com"}
    assert client.get(f"/v1/oauth/apps/{APP_ID}", headers=bob_headers).status_code == 404
    assert (
        client.patch(
            f"/v1/oauth/apps/{APP_ID}",
            headers=bob_headers,
            json={"name": "Stolen"},
        ).status_code
        == 404
    )


def test_registration_accepts_management_console_session(client: TestClient) -> None:
    _identity_user()
    _active_session(client)

    response = client.post("/v1/oauth/apps", json=_app_body())

    assert response.status_code == 201, response.text


def test_registry_rejects_management_api_key_even_for_verified_creator(
    client: TestClient,
) -> None:
    owner = _identity_user()
    workspace = STORE.list_workspaces_for_user(owner.id)[0]
    raw_key, _key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="registry management key",
        creator_user_id=owner.id,
        management=True,
    )
    headers = {"authorization": f"Bearer {raw_key}"}

    responses = [
        client.post("/v1/oauth/apps", headers=headers, json=_app_body()),
        client.get("/v1/oauth/apps", headers=headers),
        client.get(f"/v1/oauth/apps/{APP_ID}", headers=headers),
        client.patch(
            f"/v1/oauth/apps/{APP_ID}",
            headers=headers,
            json={"name": "Key-owned app"},
        ),
    ]

    assert [response.status_code for response in responses] == [403, 403, 403, 403]
    for response in responses:
        assert response.json()["error"]["type"] == "forbidden"
        assert "signed-in console session" in response.json()["error"]["message"]


def test_registry_rejects_bearer_session_without_cookie(client: TestClient) -> None:
    owner = _identity_user()
    raw_session, _session = STORE.create_auth_session(
        user_id=owner.id,
        provider="google",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )
    headers = {"authorization": f"Bearer {raw_session}"}

    responses = [
        client.post("/v1/oauth/apps", headers=headers, json=_app_body()),
        client.get("/v1/oauth/apps", headers=headers),
        client.get(f"/v1/oauth/apps/{APP_ID}", headers=headers),
        client.patch(
            f"/v1/oauth/apps/{APP_ID}",
            headers=headers,
            json={"name": "Bearer-owned app"},
        ),
    ]

    assert [response.status_code for response in responses] == [403, 403, 403, 403]
    for response in responses:
        assert response.json()["error"]["type"] == "forbidden"
        assert "signed-in console session" in response.json()["error"]["message"]


def test_registry_rejects_non_management_console_session(client: TestClient) -> None:
    owner = STORE.ensure_user("registry-owner@example.com")
    workspace = STORE.list_workspaces_for_user(owner.id)[0]
    member = STORE.add_members(workspace.id, ["registry-member@example.com"], role="member")[0]
    STORE.set_user_identity_status(
        member.user_id,
        status="approved",
        verified_name="Registry Member",
    )
    raw_session, _session = STORE.create_auth_session(
        user_id=member.user_id,
        provider="google",
        label="registry-member@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)

    response = client.post("/v1/oauth/apps", json=_app_body())

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "forbidden"


@pytest.mark.parametrize(
    ("email", "level_setup"),
    [
        ("email-only@example.com", "email"),
        ("phone-only@example.com", "phone"),
    ],
)
def test_create_rejects_email_and_phone_verification_levels(
    client: TestClient,
    email: str,
    level_setup: str,
) -> None:
    user = STORE.ensure_user(email)
    user.email_verified = True
    if level_setup == "phone":
        user.phone_verified = True
    _active_session(client, email)

    response = client.post(
        "/v1/oauth/apps",
        json=_app_body(id=f"{level_setup}-only-app"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "verification_required"
    assert "Full identity verification" in response.json()["error"]["message"]
    assert "Veriff" in response.json()["error"]["message"]


def test_patch_rechecks_identity_gate(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    user = STORE.find_user_by_email("alice@example.com")
    user.identity_status = "none"
    user.email_verified = True
    user.phone_verified = True

    response = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"name": "Must not change"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "verification_required"
    assert STORE.get_oauth_app(APP_ID).name == "Verified App"


@pytest.mark.parametrize("reserved", ["trustedrouter", "tr", "api", "console", "admin", "www"])
def test_registration_rejects_reserved_ids(
    client: TestClient,
    user_headers: dict[str, str],
    reserved: str,
) -> None:
    _identity_user()
    _active_session(client)
    response = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json=_app_body(id=reserved),
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "reserved",
    ["trustedrouter-", "trusted-router", "ТrustedRouter"],
)
def test_registration_rejects_normalized_reserved_ids(
    client: TestClient,
    user_headers: dict[str, str],
    reserved: str,
) -> None:
    _identity_user()
    _active_session(client)

    response = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json=_app_body(id=reserved),
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "id is reserved"


@pytest.mark.parametrize(
    "protected_name",
    [
        "TrustedRouter",
        "Trusted-Router",
        "ТrustedRouter",
        "TŕustedRouter",
        "TгustedRouter",
        "T R U S T E D R O U T E R",
        "trusted router inc",
        "Trust3dR0uter",
        pytest.param("Quillium Labs", id="strict-substring"),
    ],
)
def test_registration_rejects_protected_names_on_create_and_patch(
    client: TestClient,
    user_headers: dict[str, str],
    protected_name: str,
) -> None:
    _identity_user()
    _active_session(client)
    created = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json=_app_body(),
    )
    assert created.status_code == 201, created.text

    create_response = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json=_app_body(id="second-safe-app", name=protected_name),
    )
    patch_response = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"name": protected_name},
    )

    for response in (create_response, patch_response):
        assert response.status_code == 400, response.text
        assert "mistaken for TrustedRouter" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "name",
    ["Metro Labs", "Transit Tracker", "API Toolkit", "Trusty Notes"],
)
def test_registration_accepts_safe_names(
    client: TestClient,
    user_headers: dict[str, str],
    name: str,
) -> None:
    _identity_user()
    _active_session(client)

    response = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json=_app_body(name=name),
    )

    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("app_id", "expected_status"),
    [
        ("trustedrouter", 400),
        ("trustedrouter-support", 400),
        ("tr", 400),
        ("tr-tools", 400),
        ("api", 400),
        ("metrolabs", 201),
        ("transit-tracker", 201),
        ("apitoolkit", 201),
    ],
)
def test_reserved_slug_matching_uses_exact_or_term_prefix(
    client: TestClient,
    user_headers: dict[str, str],
    app_id: str,
    expected_status: int,
) -> None:
    _identity_user()
    _active_session(client)

    response = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json=_app_body(id=app_id),
    )

    assert response.status_code == expected_status, response.text


@pytest.mark.parametrize("invalid_markup", [-1, 30_001, True, "100"])
def test_patch_rejects_invalid_markup_basis_points(
    client: TestClient,
    user_headers: dict[str, str],
    invalid_markup: object,
) -> None:
    _register_app(client, user_headers)

    response = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"markup_basis_points": invalid_markup},
    )

    assert response.status_code == 400
    assert STORE.get_oauth_app(APP_ID).markup_basis_points == 30_000


@pytest.mark.parametrize(
    "override",
    [
        {"id": "ab"},
        {"id": "Bad-App"},
        {"name": ""},
        {"name": "n" * 81},
        {"redirect_uris": []},
        {"redirect_uris": [f"https://app-{index}.example/callback" for index in range(11)]},
        {"redirect_uris": ["http://remote.example/callback"]},
        {"logo_url": "http://registered.example/logo.png"},
        {"markup_basis_points": -1},
        {"markup_basis_points": 30_001},
        {"markup_basis_points": True},
        {"suspended": "false"},
    ],
)
def test_registration_validation_bounds(
    client: TestClient,
    user_headers: dict[str, str],
    override: dict[str, object],
) -> None:
    _identity_user()
    _active_session(client)
    response = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json=_app_body(**override),
    )
    assert response.status_code == 400, response.text


def test_registration_rejects_duplicate_slug_and_immutable_patch_fields(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    duplicate = client.post("/v1/oauth/apps", headers=user_headers, json=_app_body())
    immutable = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"id": "other-app"},
    )

    assert duplicate.status_code == 409
    assert immutable.status_code == 400
    assert STORE.get_oauth_app(APP_ID).id == APP_ID


def test_authorize_client_id_uses_exact_registry_identity_and_legacy_is_unchanged(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    _active_session(client)

    legacy = client.get(
        "/auth",
        params={"callback_url": "https://legacy.example/callback", "key_label": "Legacy"},
    )
    registered = client.get(
        "/auth",
        params={
            "client_id": APP_ID,
            "callback_url": CALLBACK_URL,
            "key_label": "Spoofed name",
        },
    )

    assert legacy.status_code == 200
    assert "Authorize Legacy" in legacy.text
    assert "<strong>legacy.example</strong> will receive" in legacy.text
    assert "Verified developer" not in legacy.text
    assert 'name="client_id"' not in legacy.text
    assert registered.status_code == 200, registered.text
    assert "Authorize Verified App · verified-app" in registered.text
    assert "by Alice Example (identity-verified)" in registered.text
    assert "Verified developer" not in registered.text
    assert "<h1>Authorize Spoofed name</h1>" not in registered.text
    assert 'src="https://registered.example/logo.png"' in registered.text
    assert 'referrerpolicy="no-referrer"' in registered.text
    assert 'name="client_id" value="verified-app"' in registered.text


def test_registration_and_patch_require_verified_legal_name(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    user = _identity_user(verified_name=None)
    _active_session(client)
    created = client.post("/v1/oauth/apps", headers=user_headers, json=_app_body())

    STORE.create_oauth_app(
        OAuthApp(
            id=APP_ID,
            owner_user_id=user.id,
            name="Existing App",
            redirect_uris=[CALLBACK_URL],
        )
    )
    patched = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"name": "Must not change"},
    )

    for response in (created, patched):
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "verification_required"
        assert "verified legal name" in response.json()["error"]["message"]
        assert "re-run" in response.json()["error"]["message"]

    # Defense in depth for rows predating the stronger registration gate.
    consent = client.get(
        "/auth",
        params={"client_id": APP_ID, "callback_url": CALLBACK_URL},
        follow_redirects=False,
    )

    assert consent.status_code == 403, consent.text
    assert consent.headers.get("location") is None
    assert consent.json()["error"]["type"] == "verification_required"
    assert "cannot be presented" in consent.json()["error"]["message"]
    assert "verified name is unavailable" in consent.json()["error"]["message"]
    assert "must re-verify" in consent.json()["error"]["message"]


def test_registered_consent_escapes_hostile_name_and_logo_url(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    hostile_name = '<img src=x onerror="alert(1)">'
    hostile_logo = 'https://registered.example/logo.png?x="><script>alert(2)</script>'
    _register_app(
        client,
        user_headers,
        name=hostile_name,
        logo_url=hostile_logo,
    )
    _active_session(client)

    consent = client.get(
        "/auth",
        params={"client_id": APP_ID, "callback_url": CALLBACK_URL},
    )
    rejected_logo = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"logo_url": 'javascript:alert("x")'},
    )

    assert consent.status_code == 200, consent.text
    assert hostile_name not in consent.text
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in consent.text
    assert '<script>alert(2)</script>' not in consent.text
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in consent.text
    assert rejected_logo.status_code == 400


@pytest.mark.parametrize(
    ("client_id", "callback_url"),
    [
        ("missing-app", CALLBACK_URL),
        (APP_ID, f"{CALLBACK_URL}?variant=1"),
        (APP_ID, "https://unregistered.example/callback"),
    ],
)
def test_authorize_rejects_unknown_or_non_exact_client_callback_without_redirect(
    client: TestClient,
    user_headers: dict[str, str],
    client_id: str,
    callback_url: str,
) -> None:
    _register_app(client, user_headers)
    response = client.get(
        "/auth",
        params={"client_id": client_id, "callback_url": callback_url},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.headers.get("location") is None
    assert response.headers["content-type"].startswith("application/json")


def test_authorize_rejects_suspended_app_without_redirect(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers, suspended=True)
    response = client.get(
        "/auth",
        params={"client_id": APP_ID, "callback_url": CALLBACK_URL},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.headers.get("location") is None


def test_funding_round_trip_preserves_registered_client_id(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    _active_session(client)
    client.app.state.settings.stripe_secret_key = "sk_test_oauth_app"  # noqa: S105
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_oauth_app", "url": "https://checkout.stripe.test/app"}

    with patch(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    ):
        response = client.post(
            "/auth/fund",
            data={
                "client_id": APP_ID,
                "callback_url": CALLBACK_URL,
                "key_label": "Registered app key",
                "fund_amount": "20",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    success_query = parse_qs(urlsplit(captured["success_url"]).query)
    assert success_query["client_id"] == [APP_ID]
    assert success_query["callback_url"] == [CALLBACK_URL]


def test_registered_approve_exchange_stamps_key_while_legacy_stays_empty(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    _active_session(client)
    approved = client.post(
        "/auth/approve",
        data={
            "client_id": APP_ID,
            "callback_url": CALLBACK_URL,
            "key_label": "Ignored label",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    stored_code = next(iter(STORE.oauth_code_store.codes.values()))
    assert stored_code.client_app_id == APP_ID

    exchanged = client.post("/v1/auth/keys", json={"code": code})
    key = STORE.get_key_by_raw(exchanged.json()["key"])
    assert exchanged.status_code == 200, exchanged.text
    assert key is not None and key.app_id == APP_ID
    assert exchanged.json()["data"]["app_id"] == APP_ID
    assert key_shape(key)["app_id"] == APP_ID

    legacy_approved = client.post(
        "/auth/approve",
        data={"callback_url": "https://legacy.example/callback", "key_label": "Legacy"},
        follow_redirects=False,
    )
    legacy_code = parse_qs(urlsplit(legacy_approved.headers["location"]).query)["code"][0]
    legacy_exchange = client.post("/v1/auth/keys", json={"code": legacy_code})
    legacy_key = STORE.get_key_by_raw(legacy_exchange.json()["key"])
    assert legacy_key is not None and legacy_key.app_id == ""
    assert "app_id" not in legacy_exchange.json()["data"]
    assert "app_id" not in key_shape(legacy_key)


def test_programmatic_code_client_id_requires_app_owner(
    client: TestClient,
) -> None:
    owner = _identity_user()
    _active_session(client)
    registered = client.post("/v1/oauth/apps", json=_app_body())
    assert registered.status_code == 201, registered.text

    owner_workspace = STORE.list_workspaces_for_user(owner.id)[0]
    owner_raw, _owner_key = STORE.create_api_key(
        workspace_id=owner_workspace.id,
        name="owner management",
        creator_user_id=owner.id,
        management=True,
    )
    bob = STORE.ensure_user("bob@example.com")
    bob_workspace = STORE.list_workspaces_for_user(bob.id)[0]
    bob_raw, _bob_key = STORE.create_api_key(
        workspace_id=bob_workspace.id,
        name="bob management",
        creator_user_id=bob.id,
        management=True,
    )
    code_body = {
        "client_id": APP_ID,
        "callback_url": CALLBACK_URL,
    }

    owner_code = client.post(
        "/v1/auth/keys/code",
        headers={"authorization": f"Bearer {owner_raw}"},
        json=code_body,
    )
    forged = client.post(
        "/v1/auth/keys/code",
        headers={"authorization": f"Bearer {bob_raw}"},
        json=code_body,
    )
    legacy = client.post(
        "/v1/auth/keys/code",
        headers={"authorization": f"Bearer {bob_raw}"},
        json={"callback_url": "https://legacy.example/callback"},
    )

    assert owner_code.status_code == 200, owner_code.text
    exchanged = client.post(
        "/v1/auth/keys",
        json={"code": owner_code.json()["data"]["id"]},
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["data"]["app_id"] == APP_ID
    assert forged.status_code == 403
    assert forged.json()["error"]["type"] == "forbidden"
    assert legacy.status_code == 200, legacy.text


def test_typed_authorization_freezes_app_id() -> None:
    store, database, _bigtable = make_fake_store()
    workspace_id = "ws-typed-app-attribution"
    store._write_entity("credit", workspace_id, CreditAccount(workspace_id=workspace_id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace_id, 0)] = {
        "workspace_id": workspace_id,
        "shard": 0,
        "total_credits": 5_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="app key",
        creator_user_id=None,
        app_id=APP_ID,
    )

    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=workspace_id,
        key_hash=key.hash,
        estimate=1_000,
        has_credit_candidate=True,
        reservation_usage_type=UsageType.CREDITS,
        model_id="anthropic/claude-haiku-4.5",
        provider="anthropic",
        requested_model_id="anthropic/claude-haiku-4.5",
        candidate_model_ids=["anthropic/claude-haiku-4.5"],
        region="us",
        endpoint_id="anthropic/claude-haiku-4.5@anthropic/prepaid",
        candidate_endpoint_ids=["anthropic/claude-haiku-4.5@anthropic/prepaid"],
        idempotency_key=None,
        idempotency_fingerprint=None,
        app_id=key.app_id,
        app_markup_basis_points=1_250,
        app_owner_user_id="user-app-owner",
    )

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None and authorization.app_id == APP_ID
    assert authorization.app_markup_basis_points == 1_250
    assert authorization.app_owner_user_id == "user-app-owner"
    key.app_id = "changed-after-authorize"
    assert store.get_gateway_authorization(authorization.id).app_id == APP_ID


def test_generation_settle_projection_carries_frozen_app_id_and_legacy_none() -> None:
    authorization = GatewayAuthorization(
        id="gwa-app-attribution",
        workspace_id="ws-app-attribution",
        key_hash="key-app-attribution",
        model_id="anthropic/claude-haiku-4.5",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=10,
        app_id=APP_ID,
        app_markup_basis_points=1_000,
        app_owner_user_id="user-app-owner",
    )
    generation = Generation.from_settle_body(
        authorization=authorization,
        provider_name="Anthropic",
        body={"request_id": "req-app-attribution", "elapsed_seconds": 1},
        input_tokens=1,
        output_tokens=1,
        actual_cost_microdollars=10,
        app_markup_microdollars=1,
    )
    legacy = Generation.from_settle_body(
        authorization=GatewayAuthorization(
            id="gwa-legacy-attribution",
            workspace_id="ws-app-attribution",
            key_hash="key-app-attribution",
            model_id="anthropic/claude-haiku-4.5",
            provider="anthropic",
            usage_type=UsageType.CREDITS,
            estimated_microdollars=10,
        ),
        provider_name="Anthropic",
        body={"request_id": "req-legacy-attribution", "elapsed_seconds": 1},
        input_tokens=1,
        output_tokens=1,
        actual_cost_microdollars=10,
    )

    assert generation.app_id == APP_ID
    assert generation.app_markup_microdollars == 1
    assert generation.to_openrouter_generation()["app_markup_microdollars"] == 1
    assert generation.to_openrouter_generation()["app_id"] == APP_ID
    assert generation_events([generation])[0]["app_id"] == APP_ID
    assert legacy.app_id == ""
    assert legacy.to_openrouter_generation()["app_id"] is None
    assert "app_id" not in generation_events([legacy])[0]


def test_registered_app_end_to_end_reaches_authorization_generation_and_activity(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    _active_session(client)
    approved = client.post(
        "/auth/approve",
        data={"client_id": APP_ID, "callback_url": CALLBACK_URL},
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    exchanged = client.post("/v1/auth/keys", json={"code": code})
    key = STORE.get_key_by_raw(exchanged.json()["key"])
    assert key is not None

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "idempotency_key": "oauth-app-e2e",
        },
    )
    assert authorize.status_code == 200, authorize.text
    authorization_id = authorize.json()["data"]["authorization_id"]
    authorization = STORE.get_gateway_authorization(authorization_id)
    assert authorization is not None and authorization.app_id == APP_ID

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorization_id,
            "actual_input_tokens": 1,
            "actual_output_tokens": 1,
            "request_id": "req-oauth-app-e2e",
            "elapsed_seconds": 1,
        },
    )
    assert settle.status_code == 200, settle.text
    generation = STORE.get_generation(settle.json()["data"]["generation_id"])
    assert generation is not None and generation.app_id == APP_ID

    activity = client.get("/v1/activity?group_by=none")
    assert activity.status_code == 200, activity.text
    row = next(item for item in activity.json()["data"] if item["id"] == generation.id)
    assert row["app_id"] == APP_ID


def test_suspension_after_mint_denies_gateway_and_federation_until_unsuspended(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    _active_session(client)
    approved = client.post(
        "/auth/approve",
        data={"client_id": APP_ID, "callback_url": CALLBACK_URL},
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    exchanged = client.post("/v1/auth/keys", json={"code": code})
    key = STORE.get_key_by_raw(exchanged.json()["key"])
    assert key is not None and key.app_id == APP_ID

    suspended = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"suspended": True},
    )
    assert suspended.status_code == 200, suspended.text

    denied = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "idempotency_key": "oauth-app-suspended",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["type"] == "forbidden"
    assert denied.json()["error"]["type"] != "invalid_api_key"
    assert "suspended" in denied.json()["error"]["message"]

    client.app.state.settings.federation_peer_token = "peer-secret"  # noqa: S105
    federated = client.post(
        "/v1/internal/federation/resolve-key",
        headers={
            "x-trustedrouter-federation-token": "peer-secret",
            "x-trustedrouter-federation-features": "scopes",
        },
        json={"api_key_lookup_hash": key.lookup_hash},
    )
    assert federated.status_code == 200, federated.text
    assert federated.json()["data"]["app_id"] == APP_ID
    assert federated.json()["data"]["app_suspended"] is True
    assert federated.json()["data"]["app_markup_basis_points"] == 30_000
    assert federated.json()["data"]["app_owner_user_id"] == STORE.find_user_by_email(
        user_headers["x-trustedrouter-user"]
    ).id

    unsuspended = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        headers=user_headers,
        json={"suspended": False},
    )
    assert unsuspended.status_code == 200, unsuspended.text
    restored = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "idempotency_key": "oauth-app-unsuspended",
        },
    )
    assert restored.status_code == 200, restored.text


def test_suspension_allows_identical_authorization_replay_but_denies_new_work(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _register_app(client, user_headers)
    approved = client.post(
        "/auth/approve",
        data={"client_id": APP_ID, "callback_url": CALLBACK_URL},
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    exchanged = client.post("/v1/auth/keys", json={"code": code})
    key = STORE.get_key_by_raw(exchanged.json()["key"])
    assert key is not None
    body = {
        "api_key_lookup_hash": key.lookup_hash,
        "model": "anthropic/claude-haiku-4.5",
        "estimated_input_tokens": 1,
        "max_output_tokens": 1,
        "idempotency_key": "oauth-app-replay-before-suspension",
    }

    original = client.post("/v1/internal/gateway/authorize", json=body)
    suspended = client.patch(
        f"/v1/oauth/apps/{APP_ID}",
        json={"suspended": True},
    )
    replay = client.post("/v1/internal/gateway/authorize", json=body)
    new_work = client.post(
        "/v1/internal/gateway/authorize",
        json={**body, "idempotency_key": "oauth-app-new-after-suspension"},
    )

    assert original.status_code == 200, original.text
    assert suspended.status_code == 200, suspended.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["authorization_id"] == original.json()["data"][
        "authorization_id"
    ]
    assert new_work.status_code == 403
    assert new_work.json()["error"]["type"] == "forbidden"


def test_legacy_key_avoids_app_read_and_federation_omits_empty_app_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = STORE.ensure_user("legacy-no-app@example.com")
    workspace = STORE.list_workspaces_for_user(owner.id)[0]
    _raw_key, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="legacy no app",
        creator_user_id=owner.id,
    )
    original = InMemoryStore.get_oauth_app
    app_reads = 0

    def count_app_reads(self: InMemoryStore, app_id: str):
        nonlocal app_reads
        app_reads += 1
        return original(self, app_id)

    monkeypatch.setattr(InMemoryStore, "get_oauth_app", count_app_reads)

    authorized = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "idempotency_key": "legacy-no-app-read",
        },
    )
    assert authorized.status_code == 200, authorized.text
    assert app_reads == 0

    client.app.state.settings.federation_peer_token = "peer-secret"  # noqa: S105
    served = client.post(
        "/v1/internal/federation/resolve-key",
        headers={"x-trustedrouter-federation-token": "peer-secret"},
        json={"api_key_lookup_hash": key.lookup_hash},
    )
    assert served.status_code == 200, served.text
    assert "app_id" not in served.json()["data"]
    assert "app_suspended" not in served.json()["data"]
    assert app_reads == 0


def test_non_federated_app_key_without_local_app_row_denies_authorization(
    client: TestClient,
) -> None:
    owner = STORE.ensure_user("orphaned-app-key@example.com")
    workspace = STORE.list_workspaces_for_user(owner.id)[0]
    _raw_key, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="orphaned app key",
        creator_user_id=owner.id,
        app_id="deleted-local-app",
    )

    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "idempotency_key": "orphaned-local-app",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "forbidden"
