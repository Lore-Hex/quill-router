"""The user confirms access closure, never an unsupported promise of erasure."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from trusted_router.oauth_provider import OAuthUserInfo
from trusted_router.routes.oauth import _persist_login
from trusted_router.storage import STORE
from trusted_router.storage_account_closure import AccountClosureBlocked


@pytest.fixture
def account(client):
    user = STORE.ensure_user("close@example.com", email="close@example.com", email_verified=True)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_key, key = STORE.create_api_key(workspace_id=workspace.id, name="original", creator_user_id=user.id)
    raw, session = STORE.create_auth_session(user_id=user.id, provider="github", label="close", ttl_seconds=3600)
    client.cookies.set("tr_session", raw)
    return user, workspace, raw_key, key, raw, session


def payload(client):
    page = client.get("/console/account/delete")
    assert page.status_code == 200
    assert "does not erase your personal data" in page.text
    assert page.headers["cache-control"] == "private, no-store"
    token = re.search(r'name="csrf_token" value="([a-f0-9]+)"', page.text).group(1)
    return {"csrf_token": token, "confirmation": "DELETE", "acknowledge": "yes"}


def final_payload(client):
    data = payload(client)
    review = client.post("/console/account/delete", data=data, headers={"Origin": "http://testserver"})
    assert review.status_code == 200
    assert "Final confirmation" in review.text
    assert "Your account is still active" in review.text
    assert review.headers["cache-control"] == "private, no-store"
    token = re.search(r'name="final_token" value="([a-f0-9.]+)"', review.text).group(1)
    return {"csrf_token": data["csrf_token"], "final_token": token, "confirm_close": "yes"}


def test_close_revokes_sessions_keys_and_keeps_account_tombstone(client, account):
    user, workspace, raw_key, key, raw, _ = account
    other_raw, _ = STORE.create_auth_session(user_id=user.id, provider="github", label="other", ttl_seconds=3600)
    assert '/console/account/delete' in client.get('/console/account/preferences').text
    data = final_payload(client)
    assert not STORE.get_user(user.id).disabled
    assert STORE.get_auth_session_by_raw(raw) is not None
    assert not STORE.get_key_by_hash(key.hash).disabled
    response = client.post("/console/account/delete/confirm", data=data, headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert "Account closed" in response.text
    assert "Account closure is not data erasure" in response.text
    assert "Max-Age=0" in response.headers["set-cookie"]
    closed = STORE.find_user_by_email(user.email)
    assert closed.id == user.id and closed.disabled and closed.account_closed_at
    assert STORE.get_auth_session_by_raw(raw) is None
    assert STORE.get_auth_session_by_raw(other_raw) is None
    assert STORE.session_auth_context(other_raw) is None
    assert STORE.get_key_by_hash(key.hash).disabled
    assert STORE.get_workspace(workspace.id) is None
    assert STORE.get_credit_account(workspace.id) is not None
    client.cookies.clear()
    assert client.get("/v1/keys", headers={"Authorization": f"Bearer {other_raw}"}).status_code == 401
    assert client.get("/v1/key", headers={"Authorization": f"Bearer {raw_key}"}).status_code == 401
    client.cookies.set("tr_session", raw)
    assert client.get("/console/account/preferences", follow_redirects=False).status_code == 302
    assert client.post("/console/account/delete/confirm", data=data,
                       headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 302


@pytest.mark.parametrize("origin", [None, "null", "https://evil.test", "https://testserver", "http://testserver:81"])
def test_close_rejects_missing_or_cross_origin(client, account, origin):
    headers = {"Origin": origin} if origin else {}
    assert client.post("/console/account/delete", data=payload(client), headers=headers).status_code == 403
    assert client.post("/console/account/delete/confirm", data=final_payload(client), headers=headers).status_code == 403
    assert not STORE.get_user(account[0].id).disabled


def test_close_requires_session_bound_token_and_explicit_confirmation(client, account):
    data = payload(client)
    data["csrf_token"] = "invalid"  # noqa: S105 - deliberately invalid test token
    assert client.post("/console/account/delete", data=data, headers={"Origin": "http://testserver"}).status_code == 403
    for change in ({"confirmation": "delete"}, {"acknowledge": ""}):
        result = client.post("/console/account/delete", data={**payload(client), **change},
                             headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert result.status_code == 303 and result.headers["location"].endswith("error=confirmation")
    assert not STORE.get_user(account[0].id).disabled


def test_close_cannot_target_another_user(client, account):
    victim = STORE.ensure_user("victim@example.com")
    response = client.post("/console/account/delete/confirm", data={**final_payload(client), "user_id": victim.id},
                           headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert not STORE.get_user(victim.id).disabled
    assert STORE.get_user(account[0].id).disabled


def test_shared_workspace_blocks_without_partial_closure(client, account):
    user, workspace, _, key, *_ = account
    STORE.add_members(workspace.id, ["teammate@example.com"])
    response = client.post("/console/account/delete/confirm", data=final_payload(client),
                           headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("error=shared")
    assert not STORE.get_user(user.id).disabled
    assert not STORE.get_key_by_hash(key.hash).disabled
    assert STORE.get_workspace(workspace.id)


def test_oauth_cannot_reopen_account_or_issue_starter_credit(client, account):
    user = account[0]
    STORE.close_user_account(user.id)
    info = OAuthUserInfo(sub="github-sub", email=user.email, email_verified=True, display_name="Test")
    with pytest.raises(HTTPException) as exc:
        _persist_login(SimpleNamespace(slug="github"), client.app.state.settings, info, None)
    assert exc.value.status_code == 403
    assert STORE.find_user_by_email(user.email).id == user.id
    with pytest.raises(ValueError, match="account_closed"):
        STORE.create_workspace(user.id, "Reopen")


def test_too_many_keys_blocks_without_partial_closure(client, account, monkeypatch):
    from trusted_router import storage_account_closure
    monkeypatch.setattr(storage_account_closure, "MAX_KEYS", 0)
    with pytest.raises(AccountClosureBlocked, match="assisted"):
        STORE.close_user_account(account[0].id)
    assert not STORE.get_user(account[0].id).disabled
    assert STORE.get_workspace(account[1].id)


def test_anonymous_and_api_keys_cannot_close_accounts(client, account):
    data = final_payload(client)
    client.cookies.clear()
    response = client.post("/console/account/delete/confirm", data=data,
                           headers={"Origin": "http://testserver", "Authorization": f"Bearer {account[2]}"},
                           follow_redirects=False)
    assert response.status_code == 302
    assert not STORE.get_user(account[0].id).disabled


@pytest.mark.parametrize("token", ["", "garbage", "1.invalid", "0." + "0" * 64, "\u00e9"])
def test_final_cannot_skip_first_confirmation(client, account, token):
    response = client.post("/console/account/delete/confirm",
                           data={**payload(client), "final_token": token, "confirm_close": "yes"},
                           headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("error=expired")
    assert not STORE.get_user(account[0].id).disabled


def test_final_requires_explicit_button_and_csrf(client, account):
    data = final_payload(client)
    for change, status in (({"confirm_close": ""}, 303), ({"csrf_token": "invalid"}, 403),
                           ({"csrf_token": "\u00e9"}, 403)):
        response = client.post("/console/account/delete/confirm", data={**data, **change},
                               headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert response.status_code == status
    assert not STORE.get_user(account[0].id).disabled


@pytest.mark.parametrize("elapsed", [-1, 301])
def test_final_expires_and_rejects_future_tokens(client, account, monkeypatch, elapsed):
    from trusted_router.routes.console import preferences
    data = final_payload(client)
    issued = int(data["final_token"].split(".")[0])
    monkeypatch.setattr(preferences, "time", SimpleNamespace(time=lambda: issued + elapsed))
    response = client.post("/console/account/delete/confirm", data=data,
                           headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("error=expired")
    assert not STORE.get_user(account[0].id).disabled


@pytest.mark.parametrize("different_user", [False, True])
def test_final_token_cannot_move_between_sessions_or_users(client, account, different_user):
    data = final_payload(client)
    user = STORE.ensure_user("other@example.com") if different_user else account[0]
    raw, _ = STORE.create_auth_session(user_id=user.id, provider="github", label="other", ttl_seconds=3600)
    client.cookies.set("tr_session", raw)
    data["csrf_token"] = payload(client)["csrf_token"]
    response = client.post("/console/account/delete/confirm", data=data,
                           headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303
    assert not STORE.get_user(user.id).disabled
    assert not STORE.get_user(account[0].id).disabled


def test_final_token_cannot_be_tampered_and_cancel_keeps_account(client, account):
    data = final_payload(client)
    data["final_token"] = data["final_token"][:-1] + ("1" if data["final_token"][-1] == "0" else "0")
    response = client.post("/console/account/delete/confirm", data=data,
                           headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/console/account/preferences").status_code == 200
    assert not STORE.get_user(account[0].id).disabled
