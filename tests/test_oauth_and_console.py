"""Tests for OAuth + SIWE + email verify + console-pages presence.

These tests cover commits 3-7 from the console UX overhaul. They're
grouped here rather than per-flow because the OAuth providers, wallet
flow, and console all touch the same session-cookie infrastructure and
share fixtures.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, Generation


@pytest.fixture
def google_settings() -> Settings:
    return Settings(
        environment="local",
        google_client_id="google-test-client",
        google_client_secret="google-test-secret",  # noqa: S106 - test fixture
        google_oauth_redirect_url="http://testserver/google_oauth_callback",
    )


@pytest.fixture
def github_settings() -> Settings:
    return Settings(
        environment="local",
        github_client_id="github-test-client",
        github_client_secret="github-test-secret",  # noqa: S106 - test fixture
        github_oauth_redirect_url="http://testserver/github_oauth_callback",
    )


@pytest.fixture
def google_client(google_settings: Settings) -> Iterator[TestClient]:
    app = create_app(google_settings, init_observability=False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def github_client(github_settings: Settings) -> Iterator[TestClient]:
    app = create_app(github_settings, init_observability=False)
    with TestClient(app) as client:
        yield client


# ── Provider availability ───────────────────────────────────────────────────

def test_marketing_modal_hides_disabled_providers(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    # Default settings have no Google or GitHub configured.
    assert "Continue with Google" not in page.text
    assert "Continue with GitHub" not in page.text
    # MetaMask is always available.
    assert "Continue with MetaMask" in page.text


def test_marketing_modal_renders_google_when_configured(google_client: TestClient) -> None:
    page = google_client.get("/")
    assert page.status_code == 200
    assert "Continue with Google" in page.text


def test_google_login_404s_when_unconfigured(client: TestClient) -> None:
    resp = client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code == 404


def test_github_login_404s_when_unconfigured(client: TestClient) -> None:
    resp = client.get("/auth/github/login", follow_redirects=False)
    assert resp.status_code == 404


# ── Google OAuth ────────────────────────────────────────────────────────────

def test_google_login_redirects_with_state_cookie(google_client: TestClient) -> None:
    resp = google_client.get("/auth/google/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://accounts.google.com/")
    assert "state=" in resp.headers["location"]
    assert "tr_oauth_state" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_google_oauth_helpers_use_expected_http_contract(httpx_mock) -> None:
    from trusted_router.oauth_provider import GOOGLE, exchange_code, fetch_user

    httpx_mock.add_response(
        method="POST",
        url=GOOGLE.token_url,
        json={"access_token": "google-access-token"},
    )
    token = await exchange_code(
        provider=GOOGLE,
        code="auth-code",
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106 - test fixture secret.
        redirect_uri="https://trustedrouter.com/google_oauth_callback",
    )
    assert token == "google-access-token"  # noqa: S105 - expected test token.
    token_request = httpx_mock.get_request(method="POST", url=GOOGLE.token_url)
    assert token_request is not None
    assert b"grant_type=authorization_code" in token_request.content
    assert b"code=auth-code" in token_request.content

    httpx_mock.add_response(
        method="GET",
        url="https://openidconnect.googleapis.com/v1/userinfo",
        json={
            "sub": "google-subject",
            "email": "alice@example.com",
            "email_verified": True,
            "name": "Alice",
            "picture": "https://example.test/alice.png",
        },
    )
    info = await fetch_user(provider=GOOGLE, access_token=token)
    userinfo_request = httpx_mock.get_request(
        method="GET", url="https://openidconnect.googleapis.com/v1/userinfo"
    )
    assert userinfo_request is not None
    assert userinfo_request.headers["authorization"] == "Bearer google-access-token"
    assert info.email == "alice@example.com"
    assert info.email_verified is True


def test_google_callback_rejects_state_mismatch(google_client: TestClient) -> None:
    google_client.cookies.set("tr_oauth_state", "good-state")
    resp = google_client.get(
        "/google_oauth_callback?code=abc&state=evil-state",
        follow_redirects=False,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_google_callback_creates_session_for_new_user(google_client: TestClient) -> None:
    google_client.cookies.set("tr_oauth_state", "matching-state")

    async def fake_exchange(**_: Any) -> str:
        return "access-token"  # noqa: S105

    async def fake_fetch_user(**_: Any) -> Any:
        from trusted_router.oauth_provider import OAuthUserInfo

        return OAuthUserInfo(
            sub="google-123",
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
        )

    with patch("trusted_router.routes.oauth.exchange_code", fake_exchange), \
         patch("trusted_router.routes.oauth.fetch_user", fake_fetch_user):
        resp = google_client.get(
            "/google_oauth_callback?code=auth-code&state=matching-state",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/console/welcome?first=1"
    assert "tr_session=" in resp.headers.get("set-cookie", "")
    user = STORE.find_user_by_email("alice@example.com")
    assert user is not None
    assert user.email_verified is True


@pytest.mark.asyncio
async def test_google_callback_rejects_unverified_email(google_client: TestClient) -> None:
    google_client.cookies.set("tr_oauth_state", "matching-state")

    async def fake_exchange(**_: Any) -> str:
        return "tok"  # noqa: S105

    async def fake_fetch_user(**_: Any) -> Any:
        from trusted_router.oauth_provider import OAuthUserInfo

        return OAuthUserInfo(
            sub="g-456",
            email="bob@example.com",
            email_verified=False,
            display_name=None,
        )

    with patch("trusted_router.routes.oauth.exchange_code", fake_exchange), \
         patch("trusted_router.routes.oauth.fetch_user", fake_fetch_user):
        resp = google_client.get(
            "/google_oauth_callback?code=x&state=matching-state",
            follow_redirects=False,
        )
    assert resp.status_code == 400


# ── GitHub OAuth ────────────────────────────────────────────────────────────

def test_github_login_redirects_with_state_cookie(github_client: TestClient) -> None:
    resp = github_client.get("/auth/github/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://github.com/login/oauth/authorize")


@pytest.mark.asyncio
async def test_github_oauth_helpers_use_verified_primary_email(httpx_mock) -> None:
    from trusted_router.oauth_provider import GITHUB, exchange_code, fetch_user

    github_user_url = "https://api.github.com/user"
    github_emails_url = "https://api.github.com/user/emails"

    httpx_mock.add_response(
        method="POST",
        url=GITHUB.token_url,
        json={"access_token": "github-access-token"},
    )
    token = await exchange_code(
        provider=GITHUB,
        code="github-code",
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106 - test fixture secret.
        redirect_uri="https://trustedrouter.com/github_oauth_callback",
    )
    assert token == "github-access-token"  # noqa: S105 - expected test token.
    token_request = httpx_mock.get_request(method="POST", url=GITHUB.token_url)
    assert token_request is not None
    assert b"code=github-code" in token_request.content

    httpx_mock.add_response(
        method="GET",
        url=github_user_url,
        json={
            "id": 123,
            "login": "alice",
            "email": "fallback@example.com",
            "name": "Alice",
            "avatar_url": "https://example.test/alice.png",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=github_emails_url,
        json=[
            {"email": "secondary@example.com", "primary": False, "verified": True},
            {"email": "primary@example.com", "primary": True, "verified": True},
        ],
    )
    info = await fetch_user(provider=GITHUB, access_token=token)
    assert info.email == "primary@example.com"
    assert info.email_verified is True
    for request in httpx_mock.get_requests():
        if str(request.url) in {github_user_url, github_emails_url}:
            assert request.headers["authorization"] == "Bearer github-access-token"
            assert request.headers["user-agent"] == "TrustedRouter"


@pytest.mark.asyncio
async def test_github_oauth_helper_marks_fallback_email_unverified(httpx_mock) -> None:
    from trusted_router.oauth_provider import GITHUB, fetch_user

    github_user_url = "https://api.github.com/user"
    github_emails_url = "https://api.github.com/user/emails"
    httpx_mock.add_response(
        method="GET",
        url=github_user_url,
        json={"id": 123, "login": "alice", "email": "fallback@example.com"},
    )
    httpx_mock.add_response(method="GET", url=github_emails_url, json=[])

    info = await fetch_user(provider=GITHUB, access_token="github-access-token")  # noqa: S106

    assert info.email == "fallback@example.com"
    assert info.email_verified is False


@pytest.mark.asyncio
async def test_github_callback_creates_session(github_client: TestClient) -> None:
    github_client.cookies.set("tr_oauth_state", "gh-state")

    async def fake_exchange(**_: Any) -> str:
        return "gh-tok"  # noqa: S105

    async def fake_fetch_user(**_: Any) -> Any:
        from trusted_router.oauth_provider import OAuthUserInfo

        return OAuthUserInfo(
            sub="42",
            email="gh@example.com",
            email_verified=True,
            display_name="Alice",
        )

    with patch("trusted_router.routes.oauth.exchange_code", fake_exchange), \
         patch("trusted_router.routes.oauth.fetch_user", fake_fetch_user):
        resp = github_client.get(
            "/github_oauth_callback?code=c&state=gh-state",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/console/welcome?first=1"


# ── Wallet / SIWE ───────────────────────────────────────────────────────────

def test_wallet_challenge_returns_siwe_message(client: TestClient) -> None:
    address = "0x" + "a" * 40
    resp = client.post("/v1/auth/wallet/challenge", json={"address": address})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "wants you to sign in" in data["message"]
    assert data["nonce"] in data["message"]
    assert data["expires_at"]


def test_wallet_verify_success_creates_pending_session(client: TestClient) -> None:
    private_key = "0x" + "1" * 64
    address = Account.from_key(private_key).address
    challenge = client.post("/v1/auth/wallet/challenge", json={"address": address})
    message = challenge.json()["data"]["message"]
    nonce = challenge.json()["data"]["nonce"]
    signature = Account.sign_message(encode_defunct(text=message), private_key=private_key).signature.hex()

    verify = client.post(
        "/v1/auth/wallet/verify",
        json={"address": address, "signature": "0x" + signature, "nonce": nonce},
    )
    assert verify.status_code == 200, verify.text
    data = verify.json()["data"]
    assert data["redirect"] == "/auth/wallet/email"
    assert data["state"] == "pending_email"
    assert "tr_session=" in verify.headers.get("set-cookie", "")
    user = STORE.find_user_by_wallet(address)
    assert user is not None
    assert user.email_verified is False


def test_wallet_verify_replay_rejects_second_use(client: TestClient) -> None:
    private_key = "0x" + "2" * 64
    address = Account.from_key(private_key).address
    challenge = client.post("/v1/auth/wallet/challenge", json={"address": address})
    message = challenge.json()["data"]["message"]
    nonce = challenge.json()["data"]["nonce"]
    signature = "0x" + Account.sign_message(
        encode_defunct(text=message), private_key=private_key
    ).signature.hex()
    first = client.post(
        "/v1/auth/wallet/verify",
        json={"address": address, "signature": signature, "nonce": nonce},
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/auth/wallet/verify",
        json={"address": address, "signature": signature, "nonce": nonce},
    )
    assert second.status_code == 400


def test_wallet_verify_rejects_wrong_address(client: TestClient) -> None:
    private_key = "0x" + "3" * 64
    real_address = Account.from_key(private_key).address
    fake_address = "0x" + "f" * 40
    challenge = client.post("/v1/auth/wallet/challenge", json={"address": fake_address})
    message = challenge.json()["data"]["message"]
    nonce = challenge.json()["data"]["nonce"]
    signature = "0x" + Account.sign_message(
        encode_defunct(text=message), private_key=private_key
    ).signature.hex()
    # Signed by `real_address`, but we tell the server it's `fake_address`.
    resp = client.post(
        "/v1/auth/wallet/verify",
        json={"address": fake_address, "signature": signature, "nonce": nonce},
    )
    assert resp.status_code == 400
    assert real_address  # silence unused-warning


def test_wallet_email_page_requires_pending_metamask_session(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session

    response = client.get("/auth/wallet/email")

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"


def test_wallet_email_submit_rejects_duplicate_account_email(client: TestClient) -> None:
    STORE.ensure_user("taken@example.com")
    user = STORE.create_wallet_user("0x" + "7" * 40)
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="metamask",
        label=user.wallet_address or "",
        ttl_seconds=3600,
        state="pending_email",
    )
    client.cookies.set("tr_session", raw_token)

    response = client.post("/auth/wallet/email", data={"email": "Taken@Example.com"})

    assert response.status_code == 409
    assert "already has an account" in response.text
    assert STORE.get_user(user.id).email is None


def test_wallet_email_submit_returns_dev_verify_link_and_does_not_preverify(
    client: TestClient,
) -> None:
    user = STORE.create_wallet_user("0x" + "8" * 40)
    raw_token, session = STORE.create_auth_session(
        user_id=user.id,
        provider="metamask",
        label=user.wallet_address or "",
        ttl_seconds=3600,
        state="pending_email",
    )
    client.cookies.set("tr_session", raw_token)

    response = client.post("/auth/wallet/email", data={"email": "Wallet.User@Example.com"})

    assert response.status_code == 200
    assert "Email delivery is not configured" in response.text
    updated = STORE.get_user(user.id)
    assert updated is not None
    assert updated.email == "wallet.user@example.com"
    assert updated.email_verified is False
    token_match = re.search(r"/auth/verify-email\?token=([^\"<]+)", response.text)
    assert token_match is not None

    verify = client.get(f"/auth/verify-email?token={token_match.group(1)}", follow_redirects=False)

    assert verify.status_code == 302
    assert STORE.get_user(user.id).email_verified is True
    assert STORE.get_auth_session_by_raw(raw_token).state == "active"
    assert session.hash


# ── Email verification ──────────────────────────────────────────────────────

def test_email_verify_invalid_token_404s(client: TestClient) -> None:
    resp = client.get("/auth/verify-email?token=not-a-real-token", follow_redirects=False)
    assert resp.status_code == 400
    assert "expired" in resp.text


def test_email_verify_marks_user_and_upgrades_session() -> None:
    """End-to-end: create user → make a pending session → mint token →
    visit URL → assert state flips to active and email_verified=True."""
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    with TestClient(app) as client:
        user = STORE.create_wallet_user("0x" + "9" * 40)
        STORE.set_user_email(user.id, "wallet-user@example.com")
        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="metamask",
            label="0x" + "9" * 40,
            ttl_seconds=3600,
            state="pending_email",
        )
        client.cookies.set("tr_session", raw_token)
        verify_token, _ = STORE.create_verification_token(
            user_id=user.id, purpose="signup", ttl_seconds=3600
        )
        resp = client.get(
            f"/auth/verify-email?token={verify_token}", follow_redirects=False
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/console/welcome?first=1"
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email_verified is True


# ── Console pages ───────────────────────────────────────────────────────────

@pytest.fixture
def console_session() -> tuple[TestClient, str]:
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("alice@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, raw_token


@pytest.mark.parametrize(
    "path,marker",
    [
        ("/console/api-keys", "API Keys"),
        ("/console/credits", "Credits"),
        ("/console/activity", "Recent activity"),
        ("/console/byok", "BYOK"),
        ("/console/routing", "Routing"),
        ("/console/settings", "Workspace settings"),
        ("/console/account/preferences", "Account preferences"),
    ],
)
def test_console_pages_render_with_session(
    console_session: tuple[TestClient, str], path: str, marker: str
) -> None:
    client, _ = console_session
    resp = client.get(path)
    assert resp.status_code == 200
    assert marker in resp.text
    assert "Sign out" in resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/console/api-keys",
        "/console/credits",
        "/console/activity",
        "/console/byok",
        "/console/routing",
        "/console/settings",
        "/console/account/preferences",
    ],
)
def test_console_pages_redirect_without_session(client: TestClient, path: str) -> None:
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?reason=signin"


def test_console_root_redirects_to_api_keys(console_session: tuple[TestClient, str]) -> None:
    client, _ = console_session
    resp = client.get("/console", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/console/api-keys"


def test_console_create_api_key_form_shows_raw_key_once(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session
    resp = client.post(
        "/console/api-keys",
        data={"name": "console-created", "limit": "0.000001"},
    )

    assert resp.status_code == 200
    assert "console-created" in resp.text
    assert "sk-tr-v1-" in resp.text
    workspace_id = next(iter(STORE.workspaces))
    keys = STORE.list_keys(workspace_id)
    assert len(keys) == 1
    assert keys[0].limit_microdollars == 1


def test_console_workspace_selector_persists_session_workspace(
    console_session: tuple[TestClient, str],
) -> None:
    client, raw_token = console_session
    session = STORE.get_auth_session_by_raw(raw_token)
    assert session is not None
    personal = STORE.list_workspaces_for_user(session.user_id)[0]
    org = STORE.create_workspace(owner_user_id=session.user_id, name="Org Workspace")

    page = client.get("/console/api-keys")
    assert page.status_code == 200
    assert "Org Workspace" in page.text

    selected = client.post(
        "/console/workspaces/select",
        data={"workspace_id": org.id, "next": "/console/byok"},
        follow_redirects=False,
    )
    assert selected.status_code == 303
    assert selected.headers["location"] == "/console/byok"
    refreshed = STORE.get_auth_session_by_raw(raw_token)
    assert refreshed is not None
    assert refreshed.workspace_id == org.id

    created = client.post(
        "/console/api-keys",
        data={"name": "org-key", "limit": ""},
    )
    assert created.status_code == 200
    assert [key.name for key in STORE.list_keys(org.id)] == ["org-key"]
    assert STORE.list_keys(personal.id) == []


def test_console_workspace_selector_rejects_open_redirect(
    console_session: tuple[TestClient, str],
) -> None:
    client, raw_token = console_session
    session = STORE.get_auth_session_by_raw(raw_token)
    assert session is not None
    workspace = STORE.list_workspaces_for_user(session.user_id)[0]

    selected = client.post(
        "/console/workspaces/select",
        data={"workspace_id": workspace.id, "next": "https://evil.example"},
        follow_redirects=False,
    )

    assert selected.status_code == 303
    assert selected.headers["location"] == "/console/api-keys"


def test_console_byok_form_stores_only_secret_reference_and_hint(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session
    resp = client.post(
        "/console/byok",
        data={
            "provider": "mistral",
            "secret_ref": "env://MISTRAL_API_KEY",
            "key_hint": "mis...1234",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/byok"
    page = client.get("/console/byok")
    assert "Mistral" in page.text
    assert "mis...1234" in page.text
    assert "MISTRAL_API_KEY" not in page.text


def test_console_activity_displays_microdollar_costs(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session
    workspace_id = next(iter(STORE.workspaces))
    STORE.add_generation(
        Generation(
            id="gen-tiny-cost",
            request_id="req-tiny-cost",
            workspace_id=workspace_id,
            key_hash="key_missing",
            model="openai/gpt-4o-mini",
            provider_name="OpenAI",
            app="tiny-cost-test",
            tokens_prompt=1,
            tokens_completion=1,
            total_cost_microdollars=1,
            usage_type="Credits",
            speed_tokens_per_second=1.0,
            finish_reason="stop",
            status="success",
            streamed=False,
        )
    )

    page = client.get("/console/activity")

    assert page.status_code == 200
    assert "$0.000001" in page.text
    assert "openai/gpt-4o-mini" in page.text


def test_console_routing_credits_settings_and_preferences_show_operational_controls(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session

    routing = client.get("/console/routing")
    credits = client.get("/console/credits")
    settings = client.get("/console/settings")
    preferences = client.get("/console/account/preferences")

    assert routing.status_code == 200
    assert 'model="trustedrouter/auto"' in routing.text
    assert "us-central1" in routing.text
    assert "europe-west4" in routing.text
    assert "api-europe-west4.quillrouter.com" in routing.text

    assert credits.status_code == 200
    assert "Stripe card" in credits.text
    assert "Stripe USDC / stablecoin" in credits.text
    assert "Payment methods" in credits.text
    assert "Add payment method" in credits.text
    assert "Auto-refill" in credits.text
    assert "Continue to checkout" in credits.text

    assert settings.status_code == 200
    assert "Content storage" in settings.text
    assert "does not log prompt or completion content" in settings.text

    assert preferences.status_code == 200
    assert "alice@example.com" in preferences.text
    assert "google" in preferences.text


def test_console_credits_shows_pending_stripe_setup_state(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session
    workspace_id = next(iter(STORE.workspaces))
    STORE.set_stripe_customer(workspace_id, customer_id="cus_pending_setup")

    credits = client.get("/console/credits")

    assert credits.status_code == 200
    assert "pending" in credits.text
    assert "Stripe setup is pending" in credits.text
    assert "Complete setup" in credits.text
    assert "Replace card" not in credits.text


def test_console_checkbox_inputs_are_not_full_width() -> None:
    css = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "trusted_router"
        / "static"
        / "dashboard.css"
    ).read_text(encoding="utf-8")

    assert ".checkbox-row input[type=\"checkbox\"]" in css
    assert "width:auto" in css
    assert "min-height:16px" in css


def test_console_checkout_post_does_not_404_in_local_mock_mode(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session

    resp = client.post(
        "/console/credits/checkout",
        data={"amount": "25", "payment_method": "auto"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/credits?checkout=mock"


def test_console_checkout_get_redirects_back_to_credits(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session

    resp = client.get("/console/credits/checkout", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/console/credits"


def test_console_checkout_redirects_to_stripe_stablecoin_session(monkeypatch) -> None:
    settings = Settings(
        environment="local",
        stripe_secret_key="sk_test_console_checkout",  # noqa: S106 - test fixture.
    )
    app = create_app(settings, init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("checkout@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label="checkout@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_console", "url": "https://checkout.stripe.test/session"}

    monkeypatch.setattr("trusted_router.services.stripe_billing.stripe.checkout.Session.create", create_session)

    resp = client.post(
        "/console/credits/checkout",
        data={"amount": "25", "payment_method": "stablecoin"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://checkout.stripe.test/session"
    assert captured["payment_method_types"] == ["crypto"]
    assert captured["customer_email"] == "checkout@example.com"
    assert captured["metadata"] == {
        "workspace_id": workspace.id,
        "payment_method": "stablecoin",
    }
    assert captured["success_url"].endswith("/console/credits?checkout=success")
    assert captured["cancel_url"].endswith("/console/credits?checkout=cancel")


def test_console_add_payment_method_mock_saves_method(
    console_session: tuple[TestClient, str],
) -> None:
    client, _ = console_session
    workspace_id = next(iter(STORE.workspaces))

    resp = client.post(
        "/console/credits/payment-methods/add",
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/credits?payment_method=mock"
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.stripe_customer_id
    assert account.stripe_payment_method_id
    page = client.get("/console/credits")
    assert "Replace card" in page.text
    assert "Manage in Stripe" in page.text


def test_console_add_payment_method_redirects_to_stripe_setup_session(monkeypatch) -> None:
    settings = Settings(
        environment="local",
        stripe_secret_key="sk_test_console_setup",  # noqa: S106 - test fixture.
    )
    app = create_app(settings, init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("setup@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label="setup@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_setup_console", "url": "https://checkout.stripe.test/setup"}

    monkeypatch.setattr("trusted_router.services.stripe_billing.stripe.checkout.Session.create", create_session)

    resp = client.post(
        "/console/credits/payment-methods/add",
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://checkout.stripe.test/setup"
    assert captured["mode"] == "setup"
    assert captured["payment_method_types"] == ["card"]
    assert captured["customer_email"] == "setup@example.com"
    assert captured["setup_intent_data"]["metadata"]["workspace_id"] == workspace.id
    assert captured["success_url"].endswith("/console/credits?payment_method=success")
    assert captured["cancel_url"].endswith("/console/credits?payment_method=cancel")


def test_console_manage_payment_methods_redirects_to_stripe_portal(monkeypatch) -> None:
    settings = Settings(
        environment="local",
        stripe_secret_key="sk_test_console_portal",  # noqa: S106 - test fixture.
    )
    app = create_app(settings, init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("portal@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.set_stripe_customer(
        workspace.id,
        customer_id="cus_console_portal",
        payment_method_id="pm_console_portal",
    )
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label="portal@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"url": "https://billing.stripe.test/portal"}

    monkeypatch.setattr("trusted_router.services.stripe_billing.stripe.billing_portal.Session.create", create_session)

    resp = client.post(
        "/console/credits/payment-methods/manage",
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://billing.stripe.test/portal"
    assert captured["customer"] == "cus_console_portal"
    assert captured["return_url"].endswith("/console/credits")


def test_logout_clears_cookie(console_session: tuple[TestClient, str]) -> None:
    client, _ = console_session
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "tr_session=" in set_cookie
    # The clear sets max-age=0 / expires in the past.
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


# ── Wallet auth helpers (unit tests) ────────────────────────────────────────

def test_build_siwe_message_format() -> None:
    from trusted_router.wallet_auth import build_siwe_message

    message, record = build_siwe_message(
        domain="trustedrouter.com",
        address="0x" + "a" * 40,
        nonce="abc-nonce",
        issued_at=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
    )
    assert message.startswith("trustedrouter.com wants you to sign in with your Ethereum account:")
    assert "Nonce: abc-nonce" in message
    assert "Issued At: 2026-01-02T03:04:05Z" in message
    assert record.expiration_time is not None


def test_recover_address_round_trip() -> None:
    from trusted_router.wallet_auth import build_siwe_message, recover_address

    private_key = "0x" + "5" * 64
    address = Account.from_key(private_key).address
    message, _ = build_siwe_message(
        domain="trustedrouter.com",
        address=address,
        nonce="round-trip",
        issued_at=dt.datetime.now(dt.UTC),
    )
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    recovered = recover_address(message=message, signature="0x" + signed.signature.hex())
    assert recovered == address.lower()
