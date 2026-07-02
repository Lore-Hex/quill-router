"""Console key management: disable/enable toggle, disable-first delete, and
window-limit budget edits — the form-POST -> 303 flash pattern."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.storage import STORE


@pytest.fixture
def console(client: TestClient) -> dict:
    """A signed-in console session + one API key in the workspace."""
    user = STORE.ensure_user("console-keys@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id, provider="test", label="t", ttl_seconds=3600,
        workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw_session)
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id, name="prod key", creator_user_id=user.id
    )
    return {"client": client, "workspace": workspace, "key": key}


def test_disable_enable_toggle(console: dict) -> None:
    client, key = console["client"], console["key"]
    resp = client.post(f"/console/api-keys/{key.hash}/disable", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/api-keys?saved=disabled"
    assert STORE.get_key_by_hash(key.hash).disabled is True

    page = client.get("/console/api-keys?saved=disabled")
    assert "Key disabled" in page.text  # flash banner
    assert "Enable" in page.text and "Delete" in page.text

    resp = client.post(f"/console/api-keys/{key.hash}/enable", follow_redirects=False)
    assert resp.status_code == 303
    assert STORE.get_key_by_hash(key.hash).disabled is False


def test_delete_requires_disabled_first(console: dict) -> None:
    client, key = console["client"], console["key"]
    # Active key: delete refused, key survives.
    resp = client.post(f"/console/api-keys/{key.hash}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/api-keys?error=delete-active"
    assert STORE.get_key_by_hash(key.hash) is not None

    # Disable, then delete succeeds.
    client.post(f"/console/api-keys/{key.hash}/disable", follow_redirects=False)
    resp = client.post(f"/console/api-keys/{key.hash}/delete", follow_redirects=False)
    assert resp.headers["location"] == "/console/api-keys?saved=deleted"
    assert STORE.get_key_by_hash(key.hash) is None


def test_budget_form_saves_window_limits(console: dict) -> None:
    client, key = console["client"], console["key"]
    resp = client.post(
        f"/console/api-keys/{key.hash}/limit",
        data={"limit": "100", "limit_daily": "5", "limit_weekly": "", "limit_monthly": "80"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/api-keys?saved=limit"
    updated = STORE.get_key_by_hash(key.hash)
    assert updated.limit_microdollars == 100_000_000
    assert updated.limit_daily_microdollars == 5_000_000
    assert updated.limit_weekly_microdollars is None  # empty input clears
    assert updated.limit_monthly_microdollars == 80_000_000

    # Negative rejected with the flash error, nothing changed.
    resp = client.post(
        f"/console/api-keys/{key.hash}/limit",
        data={"limit": "-1"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/console/api-keys?error=limit"
    assert STORE.get_key_by_hash(key.hash).limit_microdollars == 100_000_000

    # The page shows the window usage line for keys with window limits.
    page = client.get("/console/api-keys")
    assert "daily" in page.text and "monthly" in page.text


def test_create_form_accepts_window_limits(console: dict) -> None:
    client, workspace = console["client"], console["workspace"]
    resp = client.post(
        "/console/api-keys",
        data={"name": "windowed", "limit": "", "limit_daily": "2.50"},
    )
    assert resp.status_code == 200
    created = [k for k in STORE.list_keys(workspace.id) if k.name == "windowed"]
    assert len(created) == 1
    assert created[0].limit_microdollars is None
    assert created[0].limit_daily_microdollars == 2_500_000


def test_cross_workspace_key_is_404(console: dict, client: TestClient) -> None:
    other = STORE.ensure_user("other-ws@example.com")
    other_ws = STORE.list_workspaces_for_user(other.id)[0]
    _raw, other_key = STORE.create_api_key(
        workspace_id=other_ws.id, name="other", creator_user_id=other.id
    )
    for action in ("disable", "enable", "delete"):
        resp = console["client"].post(
            f"/console/api-keys/{other_key.hash}/{action}", follow_redirects=False
        )
        assert resp.status_code == 404


def test_member_cannot_disable_enable_or_delete(console: dict, client: TestClient) -> None:
    """codex #94: disable/enable/delete are manager-only; a plain workspace
    member gets 403 (budget edits keep pre-existing member-level access)."""
    workspace, key = console["workspace"], console["key"]
    member = STORE.add_members(workspace.id, ["member@example.com"], role="member")[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=member.user_id, provider="test", label="m", ttl_seconds=3600,
        workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw_session)
    for action in ("disable", "enable", "delete"):
        resp = client.post(f"/console/api-keys/{key.hash}/{action}", follow_redirects=False)
        assert resp.status_code == 403, (action, resp.status_code)
    assert STORE.get_key_by_hash(key.hash) is not None
    assert STORE.get_key_by_hash(key.hash).disabled is False
