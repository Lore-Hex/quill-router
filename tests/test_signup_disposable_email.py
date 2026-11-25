"""Disposable email addresses cannot create accounts on any signup path."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.oauth_provider import OAuthUserInfo
from trusted_router.signup_gate import (
    DISPOSABLE_EMAIL_MESSAGE,
    LOCAL_DISPOSABLE_EMAIL_DOMAINS,
    disposable_email_domains,
    is_disposable_email,
)
from trusted_router.storage import STORE


def _client() -> TestClient:
    settings = Settings(  # type: ignore[call-arg]
        environment="test",
        service_surface="control",
        new_signups_enabled=True,
        email_signup_enabled=True,
        google_client_id="google-test-client",
        google_client_secret="google-test-secret",  # noqa: S106
        github_client_id="github-test-client",
        github_client_secret="github-test-secret",  # noqa: S106
    )
    return TestClient(create_app(settings, configure_store_arg=False, init_observability=False))


def _oauth_callback(client: TestClient, provider: str, email: str, *, next_path: str | None = None) -> Any:
    login = client.get(
        f"/auth/{provider}/login",
        params={"next": next_path} if next_path else None,
        follow_redirects=False,
    )
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]

    async def exchange(**_: Any) -> str:
        return "access-token"  # noqa: S105

    async def fetch(**_: Any) -> OAuthUserInfo:
        return OAuthUserInfo(
            sub=f"subject-{email}",
            email=email,
            email_verified=True,
            display_name="Disposable test",
            provider_login="someone" if provider == "github" else None,
        )

    with (
        patch("trusted_router.routes.oauth.exchange_code", exchange),
        patch("trusted_router.routes.oauth.fetch_user", fetch),
    ):
        return client.get(f"/{provider}_oauth_callback?code=code&state={state}", follow_redirects=False)


def test_vendored_list_loads_and_carries_local_additions() -> None:
    domains = disposable_email_domains()
    # A real list, not an empty file that silently allows everything.
    assert len(domains) > 5_000
    assert LOCAL_DISPOSABLE_EMAIL_DOMAINS <= domains
    assert "mailinator.com" in domains
    for permanent in ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me"):
        assert permanent not in domains


@pytest.mark.parametrize(("email", "disposable"), [
    ("ring@qtymail.com", True),
    ("Ring@QTYMAIL.COM", True),
    ("ring@qtymail.com.", True),
    ("ring@sub.qtymail.com", True),
    ("someone@mailinator.com", True),
    ("someone@gmail.com", False),
    ("someone@example.com", False),
    ("someone@notqtymail.com", False),
    ("", False),
    (None, False),
    ("no-at-sign", False),
])
def test_is_disposable_email(email: str | None, disposable: bool) -> None:
    assert is_disposable_email(email) is disposable


def test_email_signup_refuses_disposable_address() -> None:
    response = _client().post("/v1/signup", json={"email": "new@qtymail.com", "name": "x"})

    assert response.status_code == 403
    assert response.json()["error"]["message"] == DISPOSABLE_EMAIL_MESSAGE
    assert STORE.find_user_by_email("new@qtymail.com") is None


def test_email_signup_still_accepts_permanent_address() -> None:
    response = _client().post("/v1/signup", json={"email": "new@example.com", "name": "x"})

    assert response.status_code == 201, response.text


@pytest.mark.parametrize("provider", ["google", "github"])
@pytest.mark.parametrize("next_path", [
    None,
    "/auth?callback_url=https%3A%2F%2Fapp.example%2Fcallback&key_label=App",
])
def test_oauth_signup_refuses_disposable_primary_email(provider: str, next_path: str | None) -> None:
    email = f"new-{provider}@qtymail.com"

    response = _oauth_callback(_client(), provider, email, next_path=next_path)

    assert response.status_code == 403
    assert response.json()["error"]["message"] == DISPOSABLE_EMAIL_MESSAGE
    assert STORE.find_user_by_email(email) is None


@pytest.mark.parametrize("provider", ["google", "github"])
def test_returning_user_on_disposable_domain_still_signs_in(provider: str) -> None:
    # Blocking creation must not lock out an account that already exists.
    email = f"returning-{provider}@qtymail.com"
    assert STORE.signup(email=email) is not None

    response = _oauth_callback(_client(), provider, email)

    assert response.status_code == 302
    assert response.headers["location"] == "/console/api-keys"


def test_invite_refuses_unknown_disposable_address_without_partial_writes() -> None:
    existing = STORE.ensure_user("existing@example.com", email="existing@example.com")
    STORE.ensure_user("owner@example.com", email="owner@example.com")
    owner = STORE.find_user_by_email("owner@example.com")
    assert owner is not None
    workspace = STORE.list_workspaces_for_user(owner.id)[0]

    response = _client().post(
        f"/v1/workspaces/{workspace.id}/members/add",
        headers={"x-trustedrouter-user": "owner@example.com"},
        json={"emails": ["existing@example.com", "invitee@qtymail.com"]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == DISPOSABLE_EMAIL_MESSAGE
    assert STORE.find_user_by_email("invitee@qtymail.com") is None
    assert not STORE.user_is_member(existing.id, workspace.id)


def test_invite_allows_existing_account_on_disposable_domain() -> None:
    existing = STORE.ensure_user("member@qtymail.com", email="member@qtymail.com")
    STORE.ensure_user("owner@example.com", email="owner@example.com")
    owner = STORE.find_user_by_email("owner@example.com")
    assert owner is not None
    workspace = STORE.list_workspaces_for_user(owner.id)[0]

    response = _client().post(
        f"/v1/workspaces/{workspace.id}/members/add",
        headers={"x-trustedrouter-user": "owner@example.com"},
        json={"emails": ["member@qtymail.com"]},
    )

    assert response.status_code == 200, response.text
    assert STORE.user_is_member(existing.id, workspace.id)
