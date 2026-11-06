"""Privileged key creation through an authenticated console session."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.storage import STORE

PATH = "/console/api-keys/management"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def owner(client: TestClient) -> dict:
    user = STORE.ensure_user("management-owner@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw, _ = STORE.create_auth_session(
        user_id=user.id, provider="test", label="management", ttl_seconds=3600,
        workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw)
    return {"user": user, "workspace": workspace, "session": raw}


def form(client: TestClient) -> dict[str, str]:
    page = client.get("/console/api-keys")
    assert page.status_code == 200
    token = BeautifulSoup(page.text, "html.parser").select_one(
        f'form[action="{PATH}"] input[name="csrf_token"]'
    )
    assert token is not None
    return {"name": "Replacement management", "csrf_token": str(token["value"]), "acknowledge": "yes"}


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_create_management_key_and_one_time_safe_reveal(client: TestClient, owner: dict, role: str) -> None:
    workspace = owner["workspace"]
    if role == "admin":
        member = STORE.add_members(workspace.id, ["manager@example.com"], role="admin")[0]
        raw, _ = STORE.create_auth_session(
            user_id=member.user_id, provider="test", label="manager", ttl_seconds=3600,
            workspace_id=workspace.id, state="active",
        )
        client.cookies.set("tr_session", raw)
    existing = {k.hash: k.disabled for k in STORE.list_keys(workspace.id)}
    response = client.post(PATH, data=form(client), headers=ORIGIN)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    soup = BeautifulSoup(response.text, "html.parser")
    secret = soup.select_one("#created-api-key").get_text()
    key = STORE.get_key_by_raw(secret)
    assert key.management is True
    assert key.workspace_id == workspace.id
    assert not key.scopes
    assert "Management key created for" in response.text
    assert "Existing keys are still active" in response.text
    assert "created-agent-message" not in response.text
    assert "Paste this short message" not in response.text
    assert 'aria-label="Copy API key"' in response.text
    assert "sk-tr-" not in response.headers.get("location", "")
    assert {k.hash: k.disabled for k in STORE.list_keys(workspace.id) if k.hash in existing} == existing
    fresh = client.get("/console/api-keys")
    assert secret not in fresh.text
    assert "Management</span>" in fresh.text


@pytest.mark.parametrize("acknowledge", [None, "", "no", "true", "on"])
def test_management_acknowledgment_is_required_server_side(client: TestClient, owner: dict, acknowledge: str | None) -> None:
    data = form(client)
    data.pop("acknowledge")
    if acknowledge is not None:
        data["acknowledge"] = acknowledge
    before = len(STORE.list_keys(owner["workspace"].id))
    response = client.post(PATH, data=data, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert "error=management-confirmation" in response.headers["location"]
    page = client.get(response.headers["location"])
    assert "Confirm the management-key permissions" in page.text
    assert '<details class="management-key-create" open>' in page.text
    assert len(STORE.list_keys(owner["workspace"].id)) == before


@pytest.mark.parametrize("origin", [None, "null", "https://testserver", "http://evil.test", "http://testserver:8888"])
def test_management_creation_requires_same_origin(client: TestClient, owner: dict, origin: str | None) -> None:
    data = form(client)
    before = len(STORE.list_keys(owner["workspace"].id))
    response = client.post(PATH, data=data, headers={"Origin": origin} if origin else {})
    assert response.status_code == 403
    assert len(STORE.list_keys(owner["workspace"].id)) == before


@pytest.mark.parametrize("token", ["", "wrong", "x" * 65])
def test_management_creation_rejects_invalid_token(client: TestClient, owner: dict, token: str) -> None:
    before = len(STORE.list_keys(owner["workspace"].id))
    response = client.post(PATH, data={"name": "Denied", "acknowledge": "yes", "csrf_token": token}, headers=ORIGIN)
    assert response.status_code == (400 if len(token) > 64 else 403)
    assert len(STORE.list_keys(owner["workspace"].id)) == before


def test_permission_revocation_hides_form_and_blocks_old_confirmation(client: TestClient, owner: dict) -> None:
    data = form(client)
    workspace = owner["workspace"]
    before = len(STORE.list_keys(workspace.id))
    STORE.add_members(workspace.id, [owner["user"].email], role="member")
    page = client.get("/console/api-keys")
    assert f'action="{PATH}"' not in page.text
    assert 'name="csrf_token"' not in page.text
    assert "Only workspace owners and managers" in page.text
    response = client.post(PATH, data=data, headers=ORIGIN)
    assert response.status_code == 403
    assert len(STORE.list_keys(workspace.id)) == before


def test_confirmation_cannot_cross_session_or_workspace(client: TestClient, owner: dict) -> None:
    data = form(client)
    second = STORE.create_workspace(owner["user"].id, "Second workspace")
    STORE.set_auth_session_workspace(owner["session"], second.id)
    before = len(STORE.list_keys(second.id))
    response = client.post(PATH, data=data, headers=ORIGIN)
    assert response.status_code == 403
    assert len(STORE.list_keys(second.id)) == before
    data = form(client)
    raw, _ = STORE.create_auth_session(
        user_id=owner["user"].id, provider="test", label="new", ttl_seconds=3600,
        workspace_id=second.id, state="active",
    )
    client.cookies.set("tr_session", raw)
    assert client.post(PATH, data=data, headers=ORIGIN).status_code == 403
    assert len(STORE.list_keys(second.id)) == before


def test_management_creation_ignores_forged_workspace(client: TestClient, owner: dict) -> None:
    other = STORE.ensure_user("other-management@example.com")
    other_ws = STORE.list_workspaces_for_user(other.id)[0]
    before = len(STORE.list_keys(other_ws.id))
    data = {**form(client), "workspace_id": other_ws.id, "creator_user_id": other.id}
    response = client.post(PATH, data=data, headers=ORIGIN)
    assert response.status_code == 200
    key = next(k for k in STORE.list_keys(owner["workspace"].id) if k.name == data["name"])
    assert key.creator_user_id == owner["user"].id
    assert len(STORE.list_keys(other_ws.id)) == before


def test_management_creation_requires_active_session(client: TestClient, owner: dict) -> None:
    data = form(client)
    before = len(STORE.list_keys(owner["workspace"].id))
    client.cookies.clear()
    response = client.post(PATH, data=data, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 302
    assert len(STORE.list_keys(owner["workspace"].id)) == before


def test_management_creation_respects_billing_pause(client: TestClient, owner: dict) -> None:
    data = form(client)
    workspace = owner["workspace"]
    before = len(STORE.list_keys(workspace.id))
    STORE.update_workspace(workspace.id, billing_paused=True)
    assert client.post(PATH, data=data, headers=ORIGIN).status_code == 503
    assert len(STORE.list_keys(workspace.id)) == before


@pytest.mark.parametrize("name", ["", "  ", "x" * 121])
def test_management_name_validation(client: TestClient, owner: dict, name: str) -> None:
    data = {**form(client), "name": name}
    before = len(STORE.list_keys(owner["workspace"].id))
    assert client.post(PATH, data=data, headers=ORIGIN).status_code == 400
    assert len(STORE.list_keys(owner["workspace"].id)) == before


def test_ordinary_creation_cannot_escalate_to_management(client: TestClient, owner: dict) -> None:
    response = client.post("/console/api-keys", data={"name": "ordinary", "management": "true"})
    assert response.status_code == 200
    key = next(k for k in STORE.list_keys(owner["workspace"].id) if k.name == "ordinary")
    assert key.management is False
    assert "created-agent-message" in response.text


def test_docs_explain_management_replacement_and_balance_access(client: TestClient) -> None:
    page = client.get("/docs")
    assert page.status_code == 200
    assert 'href="/console/api-keys#management-keys"' in page.text
    assert "Creating a replacement does not revoke old keys" in page.text
    assert "remaining_microdollars" in page.text
