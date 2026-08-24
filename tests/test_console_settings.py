"""Workspace rename from /console/settings.

This page shipped with a form posting to a route that only accepted GET, so
every Save returned 405 "Method Not Allowed" and the rename never worked. It
went unnoticed because Starlette answers 405 from the router without raising,
so Sentry (5xx-only by default) never saw it either.

These tests exercise the POST itself, which is the thing nobody was doing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.storage import STORE, InMemoryStore


@pytest.fixture
def console(client: TestClient) -> tuple[TestClient, str]:
    """A signed-in console session.

    Uses conftest's `client` fixture rather than building a fresh app:
    /console/* rejects API-key Bearer auth and wants the cookie that OAuth
    sign-in mints, but standing up a second `create_app` replaces process-wide
    settings and poisons unrelated tests.
    """
    user = STORE.ensure_user("console-settings@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id, provider="test", label="t", ttl_seconds=3600,
        workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw_session)
    return client, workspace.id


def test_post_is_routable_at_all(console: tuple[TestClient, str]) -> None:
    """The regression, stated as plainly as possible: POST must not 405.

    Asserting only on the rename would not name the original bug this directly.
    """
    client, _ = console
    response = client.post("/console/settings", data={"name": "Anything"}, follow_redirects=False)
    assert response.status_code != 405, "the Save button posts here; it must be handled"


def test_rename_persists_and_redirects(console: tuple[TestClient, str]) -> None:
    client, workspace_id = console

    response = client.post(
        "/console/settings", data={"name": "Metalcraft Inc"}, follow_redirects=False
    )

    # POST/redirect/GET, so a refresh does not re-submit.
    assert response.status_code == 303
    assert response.headers["location"] == "/console/settings?saved=1"
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None
    assert workspace.name == "Metalcraft Inc"


def test_saved_banner_renders(console: tuple[TestClient, str]) -> None:
    """A silent success is what let the 405 survive — confirm the page says so."""
    client, _ = console
    page = client.get("/console/settings?saved=1")
    assert "Workspace name saved." in page.text


@pytest.mark.parametrize(
    ("submitted", "expected_error"),
    [
        ("", "name"),
        ("   ", "name"),  # whitespace-only is empty once trimmed
        ("x" * 121, "too_long"),  # maxlength is a client hint, not a constraint
    ],
)
def test_invalid_names_are_rejected_without_changing_anything(
    console: tuple[TestClient, str], submitted: str, expected_error: str
) -> None:
    """A blank name renders as an unidentifiable workspace in the switcher, and
    this form is the only way back — so it must never be stored."""
    client, workspace_id = console
    original = STORE.get_workspace(workspace_id)
    assert original is not None
    original_name = original.name

    response = client.post(
        "/console/settings", data={"name": submitted}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/console/settings?error={expected_error}"
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None
    assert workspace.name == original_name


def test_name_is_trimmed(console: tuple[TestClient, str]) -> None:
    client, workspace_id = console

    client.post("/console/settings", data={"name": "  Padded Co  "}, follow_redirects=False)

    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None
    assert workspace.name == "Padded Co"


def test_non_manager_cannot_rename(
    console: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name appears on invoices and in every member's console, so renaming
    is a management action rather than something any member may do."""
    client, workspace_id = console
    original = STORE.get_workspace(workspace_id)
    assert original is not None
    original_name = original.name

    # Patch the CLASS, not the STORE proxy. STORE forwards via __getattr__, so
    # setattr on the proxy installs an instance attribute that monkeypatch then
    # "restores" as a bound method of the store that existed at patch time —
    # pinning it past the autouse reset_store swap and poisoning later tests.
    # conftest's auto_credit_test_workspaces patches at class level for the
    # same reason.
    monkeypatch.setattr(InMemoryStore, "user_can_manage", lambda *_a, **_k: False)

    response = client.post(
        "/console/settings", data={"name": "Hostile Rename"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/console/settings?error=forbidden"
    workspace = STORE.get_workspace(workspace_id)
    assert workspace is not None
    assert workspace.name == original_name
