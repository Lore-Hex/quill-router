"""Suggested per-window budgets: coherent ratios, shown as HINTS in the console
but NEVER applied unless the user sets a value."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.spend_windows import (
    SUGGESTED_MONTHLY_MICRODOLLARS,
    suggested_window_limits,
)
from trusted_router.storage import STORE


def test_suggested_ratios_anchor_to_the_plan() -> None:
    s = suggested_window_limits()
    assert s["monthly"] == SUGGESTED_MONTHLY_MICRODOLLARS == 200_000_000  # $200/mo plan
    assert s["weekly"] == s["monthly"] // 2 == 100_000_000  # ~half of monthly
    assert s["daily"] == s["weekly"] // 5 == 20_000_000  # ~fifth of weekly


@pytest.fixture
def console(client: TestClient) -> dict:
    user = STORE.ensure_user("suggest@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id, provider="test", label="t", ttl_seconds=3600,
        workspace_id=ws.id, state="active",
    )
    client.cookies.set("tr_session", raw_session)
    return {"client": client, "workspace": ws}


def test_console_shows_suggested_placeholders(console: dict) -> None:
    page = console["client"].get("/console/api-keys")
    assert page.status_code == 200
    # Placeholders + the balanced-starting-point note carry the $20/$100/$200 hint.
    assert 'placeholder="e.g. 20"' in page.text
    assert 'placeholder="e.g. 100"' in page.text
    assert 'placeholder="e.g. 200"' in page.text
    assert "$20 / day" in page.text and "$200 / month" in page.text


def test_suggestions_are_not_applied_by_default(console: dict) -> None:
    """The whole point: creating a key with blank window fields sets NO limits,
    even though the console suggests values."""
    client, ws = console["client"], console["workspace"]
    resp = client.post("/console/api-keys", data={"name": "no-limits", "limit": ""})
    assert resp.status_code == 200
    key = next(k for k in STORE.list_keys(ws.id) if k.name == "no-limits")
    assert key.limit_daily_microdollars is None
    assert key.limit_weekly_microdollars is None
    assert key.limit_monthly_microdollars is None
    assert key.limit_microdollars is None
