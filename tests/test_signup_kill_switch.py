from __future__ import annotations

from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.oauth_provider import OAuthUserInfo
from trusted_router.storage import STORE, InMemoryStore


def _settings(**overrides: object) -> Settings:
    return Settings(
        environment="test",
        service_surface="control",
        new_signups_enabled=False,
        email_signup_enabled=True,
        google_client_id="google-test-client",
        google_client_secret="google-test-secret",  # noqa: S106
        github_client_id="github-test-client",
        github_client_secret="github-test-secret",  # noqa: S106
        **overrides,
    )


def _client(**overrides: object) -> TestClient:
    return TestClient(
        create_app(
            _settings(**overrides),
            configure_store_arg=False,
            init_observability=False,
        )
    )


def _begin_oauth(client: TestClient, provider: str, *, next_path: str | None = None) -> str:
    response = client.get(
        f"/auth/{provider}/login",
        params={"next": next_path} if next_path else None,
        follow_redirects=False,
    )
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


async def _fake_exchange(**_: Any) -> str:
    return "access-token"  # noqa: S105


def _fake_user(email: str) -> Any:
    async def fetch(**_: Any) -> OAuthUserInfo:
        return OAuthUserInfo(
            sub=f"subject-{email}",
            email=email,
            email_verified=True,
            display_name="Signup gate test",
        )

    return fetch


def _oauth_callback(
    client: TestClient,
    provider: str,
    email: str,
    *,
    next_path: str | None = None,
) -> Any:
    state = _begin_oauth(client, provider, next_path=next_path)
    with (
        patch("trusted_router.routes.oauth.exchange_code", _fake_exchange),
        patch("trusted_router.routes.oauth.fetch_user", _fake_user(email)),
    ):
        return client.get(
            f"/{provider}_oauth_callback?code=code&state={state}",
            follow_redirects=False,
        )


def _signed_wallet_payload(client: TestClient, account: Any) -> dict[str, str]:
    challenge = client.post(
        "/v1/auth/wallet/challenge",
        json={"address": account.address},
    )
    assert challenge.status_code == 200, challenge.text
    data = challenge.json()["data"]
    signature = Account.sign_message(
        encode_defunct(text=data["message"]),
        account.key,
    ).signature.hex()
    return {
        "address": account.address,
        "signature": signature,
        "nonce": data["nonce"],
    }


def test_global_gate_blocks_email_account_creation() -> None:
    client = _client()

    response = client.post(
        "/v1/signup",
        json={"email": "new@example.com", "name": "New account"},
    )

    assert response.status_code == 403
    assert "temporarily disabled" in response.json()["error"]["message"]
    assert STORE.find_user_by_email("new@example.com") is None


@pytest.mark.parametrize("provider", ["google", "github"])
@pytest.mark.parametrize(
    "next_path",
    [
        None,
        "/auth?callback_url=https%3A%2F%2Fapp.example%2Fcallback&key_label=App",
    ],
)
def test_global_gate_blocks_first_time_oauth_including_delegated(
    provider: str,
    next_path: str | None,
) -> None:
    client = _client()

    response = _oauth_callback(
        client,
        provider,
        f"new-{provider}@example.com",
        next_path=next_path,
    )

    assert response.status_code == 403
    assert "temporarily disabled" in response.json()["error"]["message"]
    assert STORE.find_user_by_email(f"new-{provider}@example.com") is None


@pytest.mark.parametrize("provider", ["google", "github"])
def test_global_gate_keeps_returning_oauth_login_working(provider: str) -> None:
    seeded = STORE.signup(email=f"returning-{provider}@example.com")
    assert seeded is not None
    client = _client()

    response = _oauth_callback(
        client,
        provider,
        f"returning-{provider}@example.com",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/console/api-keys"
    assert "tr_session=" in response.headers.get("set-cookie", "")


def test_global_gate_blocks_first_time_wallet_before_challenge_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    account = Account.create()
    challenge_writes = 0

    def unexpected_create(*_args: object, **_kwargs: object) -> None:
        nonlocal challenge_writes
        challenge_writes += 1
        raise AssertionError("closed signup gate wrote a wallet challenge")

    monkeypatch.setattr(InMemoryStore, "create_wallet_challenge", unexpected_create)
    response = client.post(
        "/v1/auth/wallet/challenge",
        json={"address": account.address},
    )

    assert response.status_code == 403
    assert "temporarily disabled" in response.json()["error"]["message"]
    assert challenge_writes == 0
    assert STORE.find_user_by_wallet(account.address) is None


def test_global_gate_keeps_returning_wallet_login_working() -> None:
    account = Account.create()
    existing = STORE.create_wallet_user(account.address)
    client = _client()

    response = client.post(
        "/v1/auth/wallet/verify",
        json=_signed_wallet_payload(client, account),
    )

    assert response.status_code == 200, response.text
    assert STORE.find_user_by_wallet(account.address).id == existing.id
    assert "tr_session=" in response.headers.get("set-cookie", "")


def test_closed_gate_reissued_wallet_challenge_preserves_displayed_prompt() -> None:
    account = Account.create()
    existing = STORE.create_wallet_user(account.address)
    client = _client()

    first = client.post(
        "/v1/auth/wallet/challenge",
        json={"address": account.address},
    ).json()["data"]
    first_signature = Account.sign_message(
        encode_defunct(text=first["message"]),
        account.key,
    ).signature.hex()
    first_id = next(iter(STORE.wallet_challenges._challenges))

    # An unauthenticated caller races the user's displayed prompt for the same
    # known wallet. Issuance must be idempotent, not invalidate the signature.
    second = client.post(
        "/v1/auth/wallet/challenge",
        json={"address": "0x" + account.address[2:].upper()},
    ).json()["data"]
    second_id = next(iter(STORE.wallet_challenges._challenges))

    accepted = client.post(
        "/v1/auth/wallet/verify",
        json={
            "address": account.address,
            "signature": first_signature,
            "nonce": first["nonce"],
        },
    )

    assert second["nonce"] == first["nonce"]
    assert second["message"] == first["message"]
    assert second_id == first_id
    assert accepted.status_code == 200, accepted.text
    assert STORE.find_user_by_wallet(account.address).id == existing.id


def test_global_gate_blocks_inviting_an_unknown_email_without_partial_writes() -> None:
    existing = STORE.ensure_user("existing@example.com", email="existing@example.com")
    client = _client()
    owner = STORE.ensure_user("owner@example.com", email="owner@example.com")
    workspace = STORE.list_workspaces_for_user(owner.id)[0]

    response = client.post(
        f"/v1/workspaces/{workspace.id}/members/add",
        headers={"x-trustedrouter-user": "owner@example.com"},
        json={"emails": ["existing@example.com", "unknown@example.com"]},
    )

    assert response.status_code == 403
    assert STORE.find_user_by_email("unknown@example.com") is None
    assert not STORE.user_is_member(existing.id, workspace.id)


def test_global_gate_allows_inviting_an_existing_account() -> None:
    existing = STORE.ensure_user("existing@example.com", email="existing@example.com")
    client = _client()
    owner = STORE.ensure_user("owner@example.com", email="owner@example.com")
    workspace = STORE.list_workspaces_for_user(owner.id)[0]

    response = client.post(
        f"/v1/workspaces/{workspace.id}/members/add",
        headers={"x-trustedrouter-user": "owner@example.com"},
        json={"emails": ["existing@example.com"]},
    )

    assert response.status_code == 200, response.text
    assert STORE.user_is_member(existing.id, workspace.id)
