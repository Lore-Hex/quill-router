"""Wallet-only accounts attach and verify an email from an ACTIVE wallet session.

Wallet sign-in mints active sessions, and the billing and BYOK refusals tell
wallet-only users to "add and verify an email". These tests drive that path
over HTTP: real SIWE sign-in, the attach form, the emailed link, then the
gated features.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from trusted_router.billing_policy import is_wallet_only_user
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, User

PRIVATE_KEY = "0x" + "4" * 64


@pytest.fixture
def wallet_client() -> Iterator[tuple[TestClient, User]]:
    app = create_app(
        Settings(environment="local", wallet_signup_enabled=True),
        init_observability=False,
    )
    with TestClient(app) as client:
        address = Account.from_key(PRIVATE_KEY).address
        challenge = client.post("/v1/auth/wallet/challenge", json={"address": address})
        assert challenge.status_code == 200, challenge.text
        data = challenge.json()["data"]
        signature = Account.sign_message(
            encode_defunct(text=data["message"]), private_key=PRIVATE_KEY
        ).signature.hex()
        verify = client.post(
            "/v1/auth/wallet/verify",
            json={"address": address, "signature": "0x" + signature, "nonce": data["nonce"]},
        )
        assert verify.status_code == 200, verify.text
        assert verify.json()["data"]["state"] == "active"
        user = STORE.find_user_by_wallet(address)
        assert user is not None
        assert is_wallet_only_user(user)
        yield client, user


def _csrf(client: TestClient) -> str:
    form = client.get("/auth/wallet/email")
    assert form.status_code == 200, form.text
    match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
    assert match is not None
    return match.group(1)


def _request_link(client: TestClient, email: str) -> str:
    response = client.post(
        "/auth/wallet/email", data={"email": email, "csrf_token": _csrf(client)}
    )
    assert response.status_code == 200, response.text
    match = re.search(r'href="([^"]*/auth/wallet/email/verify\?[^"]+)"', response.text)
    assert match is not None, response.text
    return html.unescape(match.group(1))


def test_active_wallet_session_attaches_verified_email_and_unlocks_byok(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client

    # The console pages that carry the wallet-only refusals link to the flow.
    assert 'href="/auth/wallet/email"' in client.get("/console/byok").text
    assert 'href="/auth/wallet/email"' in client.get("/console/credits").text

    form = client.get("/auth/wallet/email")
    assert form.status_code == 200
    assert "Add and verify an email" in form.text

    link = _request_link(client, "Wallet.Owner@Example.com")

    # Requesting the link changes nothing: the email only counts once verified.
    pending = STORE.get_user(user.id)
    assert pending is not None
    assert pending.email is None
    assert is_wallet_only_user(pending)
    assert STORE.find_user_by_email("wallet.owner@example.com") is None

    verify = client.get(link, follow_redirects=False)

    assert verify.status_code == 302, verify.text
    assert verify.headers["location"] == "/console/credits"
    verified = STORE.get_user(user.id)
    assert verified is not None
    assert verified.email == "wallet.owner@example.com"
    assert verified.email_verified is True
    assert not is_wallet_only_user(verified)
    owner = STORE.find_user_by_email("wallet.owner@example.com")
    assert owner is not None and owner.id == user.id

    # The link is one-shot.
    assert client.get(link, follow_redirects=False).status_code == 400

    workspace = STORE.list_workspaces_for_user(user.id)[0]
    saved = client.post(
        "/console/byok",
        data={"provider": "openai", "api_key": "sk-test-0123456789abcdef"},
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    assert "error" not in saved.headers["location"]
    assert [p.provider for p in STORE.list_byok_providers(workspace.id)] == ["openai"]

    # The attach prompt is gone once the account is no longer wallet-only.
    assert 'href="/auth/wallet/email"' not in client.get("/console/byok").text
    assert 'href="/auth/wallet/email"' not in client.get("/console/credits").text
    already = client.get("/auth/wallet/email", follow_redirects=False)
    assert already.status_code == 303
    assert already.headers["location"] == "/console/credits"


def test_attach_refuses_an_email_another_account_owns(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client
    other = STORE.ensure_user("taken@example.com", email="taken@example.com", email_verified=True)

    response = client.post(
        "/auth/wallet/email",
        data={"email": "Taken@Example.com", "csrf_token": _csrf(client)},
    )

    assert response.status_code == 409
    assert "already has an account" in response.text
    assert "/auth/wallet/email/verify" not in response.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None
    assert is_wallet_only_user(refreshed)
    owner = STORE.find_user_by_email("taken@example.com")
    assert owner is not None and owner.id == other.id


def test_verify_refuses_an_email_claimed_after_the_link_was_sent(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client
    link = _request_link(client, "race@example.com")
    other = STORE.ensure_user("race@example.com", email="race@example.com", email_verified=True)

    verify = client.get(link, follow_redirects=False)

    assert verify.status_code == 409
    assert "already has an account" in verify.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None
    assert is_wallet_only_user(refreshed)
    owner = STORE.find_user_by_email("race@example.com")
    assert owner is not None and owner.id == other.id and owner.email_verified


def test_link_is_bound_to_the_address_it_was_sent_to(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client
    link = _request_link(client, "mine@example.com")
    tampered = link.replace("email=mine%40example.com", "email=victim%40example.com")
    assert tampered != link

    response = client.get(tampered, follow_redirects=False)

    assert response.status_code == 400
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None
    assert STORE.find_user_by_email("victim@example.com") is None


def test_unverified_request_does_not_reserve_the_address(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client
    _request_link(client, "someone@example.com")

    # Nothing claims the address, so its real owner signing up later gets a
    # separate account instead of landing in this wallet account.
    assert STORE.find_user_by_email("someone@example.com") is None
    owner = STORE.ensure_user("someone@example.com", email="someone@example.com")
    assert owner.id != user.id


def test_submit_requires_the_session_bound_csrf_token(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client

    missing = client.post("/auth/wallet/email", data={"email": "csrf@example.com"})
    wrong = client.post(
        "/auth/wallet/email", data={"email": "csrf@example.com", "csrf_token": "0" * 64}
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert "/auth/wallet/email/verify" not in missing.text + wrong.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None


def test_verification_sends_are_capped_per_user(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, _user = wallet_client
    for index in range(5):
        _request_link(client, f"cap{index}@example.com")

    capped = client.post(
        "/auth/wallet/email",
        data={"email": "cap5@example.com", "csrf_token": _csrf(client)},
    )

    assert capped.status_code == 429
    assert "/auth/wallet/email/verify" not in capped.text


def test_second_link_cannot_replace_an_already_verified_email(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client
    first = _request_link(client, "first@example.com")
    second = _request_link(client, "second@example.com")

    assert client.get(first, follow_redirects=False).status_code == 302
    replaced = client.get(second, follow_redirects=False)

    assert replaced.status_code == 409
    assert "already has a verified email" in replaced.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None
    assert refreshed.email == "first@example.com" and refreshed.email_verified
    assert STORE.find_user_by_email("second@example.com") is None


def test_undelivered_email_never_shows_the_link_outside_local(
    wallet_client: tuple[TestClient, User], monkeypatch: pytest.MonkeyPatch
) -> None:
    from trusted_router.routes import wallet_oauth

    client, user = wallet_client
    monkeypatch.setattr(wallet_oauth, "_inline_link_allowed", lambda _settings: False)

    response = client.post(
        "/auth/wallet/email",
        data={"email": "nodelivery@example.com", "csrf_token": _csrf(client)},
    )

    assert response.status_code == 503
    assert "/auth/wallet/email/verify" not in response.text
    assert "token=" not in response.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None


def test_non_wallet_session_cannot_use_the_attach_flow() -> None:
    app = create_app(
        Settings(environment="local", wallet_signup_enabled=True),
        init_observability=False,
    )
    with TestClient(app) as client:
        user = STORE.ensure_user("alice@example.com", email="alice@example.com", email_verified=True)
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        raw, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="google",
            label="alice@example.com",
            workspace_id=workspace.id,
            ttl_seconds=3600,
            state="active",
        )
        client.cookies.set("tr_session", raw)

        form = client.get("/auth/wallet/email")
        submit = client.post(
            "/auth/wallet/email", data={"email": "new@example.com", "csrf_token": "x"}
        )

    assert form.status_code == 401
    assert submit.status_code == 401
