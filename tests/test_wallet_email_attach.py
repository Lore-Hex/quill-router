"""Wallet-only accounts attach and verify an email from an ACTIVE wallet session.

Wallet sign-in mints active sessions, and the billing and BYOK refusals tell
wallet-only users to "add and verify an email". These tests drive that path
over HTTP: real SIWE sign-in, the attach form, the emailed code, then the
gated features.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Iterator

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from tests.wallet_email_helpers import WalletMailbox
from trusted_router.billing_policy import is_wallet_only_user
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes import wallet_oauth
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


def test_disposable_address_cannot_upgrade_a_wallet(wallet_client: tuple[TestClient, User]) -> None:
    client, user = wallet_client
    result = client.post("/auth/wallet/email", data={"email": "new@qtymail.com", "csrf_token": _csrf(client)})
    assert result.status_code == 400
    assert is_wallet_only_user(STORE.get_user(user.id))


def _csrf(client: TestClient) -> str:
    form = client.get("/auth/wallet/email")
    assert form.status_code == 200, form.text
    match = re.search(r'name="csrf_token" value="([^"]+)"', form.text)
    assert match is not None
    return match.group(1)


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> WalletMailbox:
    box = WalletMailbox()
    monkeypatch.setattr(wallet_oauth, "get_email_service", lambda _: box)
    return box


def _request_code(client: TestClient, email: str, box: WalletMailbox) -> dict[str, str]:
    response = client.post(
        "/auth/wallet/email", data={"email": email, "csrf_token": _csrf(client)}
    )
    assert response.status_code == 200, response.text
    return box.confirmation(response.text)


def _confirm(client: TestClient, fields: dict[str, str]):
    return client.post("/auth/wallet/email/verify", data=fields, follow_redirects=False)


def test_active_wallet_session_attaches_verified_email_and_unlocks_byok(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, user = wallet_client

    # The console pages that carry the wallet-only refusals link to the flow.
    assert 'href="/auth/wallet/email"' in client.get("/console/byok").text
    assert 'href="/auth/wallet/email"' in client.get("/console/credits").text

    form = client.get("/auth/wallet/email")
    assert form.status_code == 200
    assert "Add and verify an email" in form.text

    fields = _request_code(client, "Wallet.Owner@Example.com", mailbox)

    # Requesting the code changes nothing: the email only counts once verified.
    pending = STORE.get_user(user.id)
    assert pending is not None
    assert pending.email is None
    assert is_wallet_only_user(pending)
    assert STORE.find_user_by_email("wallet.owner@example.com") is None

    verify = _confirm(client, fields)

    assert verify.status_code == 302, verify.text
    assert verify.headers["location"] == "/console/credits"
    verified = STORE.get_user(user.id)
    assert verified is not None
    assert verified.email == "wallet.owner@example.com"
    assert verified.email_verified is True
    assert not is_wallet_only_user(verified)
    owner = STORE.find_user_by_email("wallet.owner@example.com")
    assert owner is not None and owner.id == user.id

    # The code is one-shot.
    assert _confirm(client, fields).status_code == 400

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


def test_attach_does_not_reveal_that_an_email_has_an_account(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox
) -> None:
    client, user = wallet_client
    other = STORE.ensure_user("taken@example.com", email="taken@example.com", email_verified=True)
    taken = client.post(
        "/auth/wallet/email",
        data={"email": "Taken@Example.com", "csrf_token": _csrf(client)},
    )
    free = client.post(
        "/auth/wallet/email",
        data={"email": "free@example.com", "csrf_token": _csrf(client)},
    )

    # The same page and status either way: the form is not an account oracle.
    assert taken.status_code == free.status_code == 200
    assert "already has an account" not in taken.text
    def normalized(page: str, email: str) -> str:
        return re.sub(r'(name="challenge" value=")[^"]+', r'\g<1>X', page.replace(email, "X"))

    assert normalized(taken.text, "taken@example.com") == normalized(free.text, "free@example.com")
    notice, link = mailbox.sent
    assert notice.to == "taken@example.com"
    assert notice.mail_class == "email_in_use_notice"
    assert "/auth/wallet/email/verify" not in notice.text_body + (notice.html_body or "")
    assert link.to == "free@example.com"
    assert "verification code" in link.text_body and "/auth/wallet/email/verify" not in link.text_body
    # Nothing was attached and no token exists for the taken address.
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None
    assert is_wallet_only_user(refreshed)
    owner = STORE.find_user_by_email("taken@example.com")
    assert owner is not None and owner.id == other.id


def test_legacy_wallet_links_are_inert(wallet_client: tuple[TestClient, User]) -> None:
    client, user = wallet_client
    email = "ordinary@example.com"
    purpose = "wallet_email_attach:" + hashlib.sha256(email.encode()).hexdigest()
    token, _ = STORE.create_verification_token(user_id=user.id, purpose=purpose, ttl_seconds=900)
    for prefix in ("", "/v1"):
        result = client.get(prefix + "/auth/wallet/email/verify", params={"token": token, "email": email})
        assert result.status_code == 410
    assert STORE.find_user_by_email(email) is None
    assert STORE.consume_verification_token(token, purpose=purpose) is not None


def test_taken_address_counts_against_the_send_cap(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox
) -> None:
    client, _user = wallet_client
    STORE.ensure_user("taken@example.com", email="taken@example.com", email_verified=True)
    for _ in range(5):
        response = client.post(
            "/auth/wallet/email",
            data={"email": "taken@example.com", "csrf_token": _csrf(client)},
        )
        assert response.status_code == 200

    capped = client.post(
        "/auth/wallet/email",
        data={"email": "free@example.com", "csrf_token": _csrf(client)},
    )

    assert capped.status_code == 429


def test_verify_refuses_an_email_claimed_after_the_code_was_sent(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, user = wallet_client
    fields = _request_code(client, "race@example.com", mailbox)
    other = STORE.ensure_user("race@example.com", email="race@example.com", email_verified=True)

    verify = _confirm(client, fields)
    assert verify.status_code == 409
    assert "already has an account" in verify.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None
    assert is_wallet_only_user(refreshed)
    owner = STORE.find_user_by_email("race@example.com")
    assert owner is not None and owner.id == other.id and owner.email_verified


def test_code_is_bound_to_the_address_it_was_sent_to(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, user = wallet_client
    fields = _request_code(client, "mine@example.com", mailbox)
    response = _confirm(client, {**fields, "email": "victim@example.com"})
    assert response.status_code == 400
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None
    assert STORE.find_user_by_email("victim@example.com") is None


def test_unverified_request_does_not_reserve_the_address(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, user = wallet_client
    _request_code(client, "someone@example.com", mailbox)

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
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, _user = wallet_client
    for index in range(5):
        _request_code(client, f"cap{index}@example.com", mailbox)

    capped = client.post(
        "/auth/wallet/email",
        data={"email": "cap5@example.com", "csrf_token": _csrf(client)},
    )

    assert capped.status_code == 429
    assert "/auth/wallet/email/verify" not in capped.text


def test_second_code_cannot_replace_an_already_verified_email(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, user = wallet_client
    first = _request_code(client, "first@example.com", mailbox)
    second = _request_code(client, "second@example.com", mailbox)

    assert _confirm(client, first).status_code == 302
    replaced = _confirm(client, second)

    assert replaced.status_code == 409
    assert "already has a verified email" in replaced.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None
    assert refreshed.email == "first@example.com" and refreshed.email_verified
    assert STORE.find_user_by_email("second@example.com") is None


def test_undelivered_email_never_shows_a_code_even_in_local_mode(
    wallet_client: tuple[TestClient, User],
) -> None:
    client, user = wallet_client
    response = client.post(
        "/auth/wallet/email",
        data={"email": "nodelivery@example.com", "csrf_token": _csrf(client)},
    )

    assert response.status_code == 503
    assert "/auth/wallet/email/verify" not in response.text
    assert "token=" not in response.text
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None and refreshed.email is None


def test_confirmation_requires_original_wallet_session_and_csrf(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, user = wallet_client
    fields = _request_code(client, "this-wallet@example.com", mailbox)
    original = client.cookies.get("tr_session")
    client.cookies.clear()
    assert _confirm(client, fields).status_code == 401
    for other in (user, STORE.create_wallet_user("0x" + "b" * 40)):
        raw, _ = STORE.create_auth_session(user_id=other.id, provider="metamask",
                                          label=other.wallet_address or "", ttl_seconds=3600, state="active")
        client.cookies.set("tr_session", raw)
        assert _confirm(client, fields).status_code == 403
        assert _confirm(client, {**fields, "csrf_token": _csrf(client)}).status_code == 400
    client.cookies.set("tr_session", original)
    assert _confirm(client, {**fields, "csrf_token": ""}).status_code == 403
    assert STORE.find_user_by_email(fields["email"]) is None
    assert _confirm(client, fields).status_code == 302


def test_confirmation_attempts_are_bounded(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, _ = wallet_client
    fields = _request_code(client, "limited@example.com", mailbox)
    wrong = f"{(int(fields['code']) + 1) % 100_000_000:08d}"
    for _ in range(5):
        assert _confirm(client, {**fields, "code": wrong}).status_code == 400
    assert _confirm(client, fields).status_code == 429
    assert STORE.find_user_by_email(fields["email"]) is None


def test_expired_code_cannot_attach(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_models

    client, _ = wallet_client
    fields = _request_code(client, "expired@example.com", mailbox)
    later = storage_models.utcnow() + dt.timedelta(minutes=11)
    monkeypatch.setattr(storage_models, "utcnow", lambda: later)
    assert _confirm(client, fields).status_code == 400
    assert STORE.find_user_by_email(fields["email"]) is None


def test_recipient_quota_spans_different_wallet_accounts(
    wallet_client: tuple[TestClient, User], mailbox: WalletMailbox,
) -> None:
    client, _ = wallet_client
    for digit in "123456":
        user = STORE.create_wallet_user("0x" + digit * 40)
        raw, _ = STORE.create_auth_session(user_id=user.id, provider="metamask",
                                          label=user.wallet_address or "", ttl_seconds=3600, state="active")
        client.cookies.clear()
        client.cookies.set("tr_session", raw)
        response = client.post("/auth/wallet/email", data={"email": "Recipient@Example.com", "csrf_token": _csrf(client)})
        assert response.status_code == 200 and 'name="challenge"' in response.text
    assert len(mailbox.sent) == 5
    assert STORE.find_user_by_email("recipient@example.com") is None
    assert _request_code(client, "other-recipient@example.com", mailbox)


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
