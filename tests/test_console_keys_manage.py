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
    member gets 403. Budget edits and key creation are manager-only too (see
    the spend-cap tests below)."""
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


_CAPPED = {
    "limit_microdollars": 10_000_000,
    "limit_daily_microdollars": 1_000_000,
    "limit_weekly_microdollars": 3_000_000,
    "limit_monthly_microdollars": 8_000_000,
    "budget_alert_only": False,
}

# Each form a plain member could post to escape a cap: clear every limit,
# raise every limit, or keep the numbers and switch to alert-only (no block).
_ESCAPES = {
    "clear": {"limit": "", "limit_daily": "", "limit_weekly": "", "limit_monthly": ""},
    "raise": {
        "limit": "100000", "limit_daily": "100000",
        "limit_weekly": "100000", "limit_monthly": "100000",
    },
    "alert-only": {
        "limit": "10", "limit_daily": "1", "limit_weekly": "3",
        "limit_monthly": "8", "budget_alert_only": "on",
    },
}


def _sign_in(client: TestClient, workspace_id: str, user_id: str) -> str:
    raw_session, _ = STORE.create_auth_session(
        user_id=user_id, provider="test", label="s", ttl_seconds=3600,
        workspace_id=workspace_id, state="active",
    )
    client.cookies.set("tr_session", raw_session)
    return raw_session


def _caps(key_hash: str) -> dict:
    key = STORE.get_key_by_hash(key_hash)
    assert key is not None
    return {field: getattr(key, field) for field in _CAPPED}


@pytest.mark.parametrize("escape", sorted(_ESCAPES))
@pytest.mark.parametrize("creator", ["owner", "member"])
def test_member_cannot_lift_a_capped_key_budget(
    console: dict, client: TestClient, creator: str, escape: str
) -> None:
    """A plain member cannot clear, raise or switch to alert-only the budget
    on a key an owner capped, whether the key is the member's own or not."""
    workspace = console["workspace"]
    member = STORE.add_members(workspace.id, ["capped@example.com"], role="member")[0]
    creator_id = member.user_id if creator == "member" else workspace.owner_user_id
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id, name="capped", creator_user_id=creator_id, **_CAPPED,
    )
    _sign_in(client, workspace.id, member.user_id)

    resp = client.post(
        f"/console/api-keys/{key.hash}/limit", data=_ESCAPES[escape], follow_redirects=False,
    )

    assert resp.status_code == 403
    assert _caps(key.hash) == _CAPPED


def test_member_cannot_bypass_caps_with_a_new_key(console: dict, client: TestClient) -> None:
    """Creating a fresh uncapped key is the same escape, so it is manager-only."""
    workspace = console["workspace"]
    member = STORE.add_members(workspace.id, ["new-key@example.com"], role="member")[0]
    _sign_in(client, workspace.id, member.user_id)
    before = {k.hash for k in STORE.list_keys(workspace.id)}

    for data in ({"name": "uncapped"}, {"name": "capped", "limit": "1", "limit_daily": "1"}):
        resp = client.post("/console/api-keys", data=data, follow_redirects=False)
        assert resp.status_code == 403, data

    assert {k.hash for k in STORE.list_keys(workspace.id)} == before


def test_member_page_shows_budgets_without_edit_or_create_forms(
    console: dict, client: TestClient
) -> None:
    workspace, key = console["workspace"], console["key"]
    STORE.update_key(key.hash, {"limit_microdollars": 10_000_000})
    member = STORE.add_members(workspace.id, ["viewer@example.com"], role="member")[0]
    _sign_in(client, workspace.id, member.user_id)

    page = client.get("/console/api-keys")

    assert page.status_code == 200
    assert "prod key" in page.text and "of $10.00" in page.text
    assert 'action="/console/api-keys"' not in page.text
    assert f'action="/console/api-keys/{key.hash}/limit"' not in page.text
    assert f'action="/console/api-keys/{key.hash}/disable"' not in page.text
    assert 'data-action="open-new-key"' not in page.text
    assert "Only workspace owners and managers can create keys or change budgets" in page.text


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_managers_can_still_change_budgets_and_create_keys(
    console: dict, client: TestClient, role: str
) -> None:
    workspace = console["workspace"]
    if role == "admin":
        admin = STORE.add_members(workspace.id, ["boss@example.com"], role="admin")[0]
        _sign_in(client, workspace.id, admin.user_id)
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id, name="capped", creator_user_id=workspace.owner_user_id,
        **_CAPPED,
    )

    page = client.get("/console/api-keys")
    assert f'action="/console/api-keys/{key.hash}/limit"' in page.text
    assert 'action="/console/api-keys"' in page.text

    resp = client.post(
        f"/console/api-keys/{key.hash}/limit", data=_ESCAPES["clear"], follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/api-keys?saved=limit"
    assert STORE.get_key_by_hash(key.hash).limit_microdollars is None

    resp = client.post("/console/api-keys", data={"name": f"{role}-made"})
    assert resp.status_code == 200
    assert any(k.name == f"{role}-made" for k in STORE.list_keys(workspace.id))


@pytest.mark.parametrize("transport", ["cookie", "bearer"])
def test_api_key_routes_apply_the_same_member_rule(
    console: dict, client: TestClient, transport: str
) -> None:
    """The /v1/keys routes already require the manager role for a session; a
    member session gets the same answer there as in the console."""
    workspace = console["workspace"]
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id, name="capped", creator_user_id=workspace.owner_user_id,
        **_CAPPED,
    )

    def as_role(role: str) -> dict[str, str]:
        added = STORE.add_members(workspace.id, [f"api-{role}@example.com"], role=role)[0]
        raw_session = _sign_in(client, workspace.id, added.user_id)
        if transport == "cookie":
            return {}
        client.cookies.clear()
        return {"Authorization": f"Bearer {raw_session}"}

    headers = as_role("member")
    for body in (
        {"limit": None, "limit_daily": None, "limit_weekly": None, "limit_monthly": None},
        {"limit": "100000"},
        {"budget_alert_only": True},
    ):
        resp = client.patch(f"/v1/keys/{key.hash}", json=body, headers=headers)
        assert resp.status_code == 403, body
    assert client.post("/v1/keys", json={"name": "uncapped"}, headers=headers).status_code == 403
    assert _caps(key.hash) == _CAPPED

    headers = as_role("admin")
    resp = client.patch(f"/v1/keys/{key.hash}", json={"limit": "20"}, headers=headers)
    assert resp.status_code == 200
    assert STORE.get_key_by_hash(key.hash).limit_microdollars == 20_000_000


def test_create_form_budget_alert_only_checkbox(console: dict) -> None:
    client, workspace = console["client"], console["workspace"]
    page = client.get("/console/api-keys").text
    assert '<input type="checkbox" name="budget_alert_only">' in page
    assert '<input type="checkbox" name="budget_alert_only" checked>' not in page
    assert "Budgets hard-stop requests with 429 by default" in page
    # Alert-only mode requires an explicit checked value.
    client.post("/console/api-keys", data={"name": "alerted", "budget_alert_only": "on"})
    alerted = next(k for k in STORE.list_keys(workspace.id) if k.name == "alerted")
    assert alerted.budget_alert_only is True
    # The normal omitted field creates a hard-limit key.
    client.post("/console/api-keys", data={"name": "hardlimit"})
    hard = next(k for k in STORE.list_keys(workspace.id) if k.name == "hardlimit")
    assert hard.budget_alert_only is False


def test_limit_form_toggles_alert_vs_limit(console: dict) -> None:
    client, key = console["client"], console["key"]
    # Uncheck Alert only (field omitted) -> hard limit.
    client.post(f"/console/api-keys/{key.hash}/limit", data={"limit": ""}, follow_redirects=False)
    assert STORE.get_key_by_hash(key.hash).budget_alert_only is False
    # Check it back -> alert mode.
    client.post(
        f"/console/api-keys/{key.hash}/limit",
        data={"limit": "", "budget_alert_only": "on"}, follow_redirects=False,
    )
    assert STORE.get_key_by_hash(key.hash).budget_alert_only is True
    # The checkbox renders on the page.
    assert "Email alert only" in client.get("/console/api-keys").text


def test_budget_can_be_cleared_back_to_undefined(console: dict) -> None:
    """Blank inputs unset a budget (back to undefined) — the clear path."""
    client, key = console["client"], console["key"]
    # Set some budgets first.
    client.post(
        f"/console/api-keys/{key.hash}/limit",
        data={"limit": "100", "limit_daily": "5", "limit_monthly": "80"},
        follow_redirects=False,
    )
    k = STORE.get_key_by_hash(key.hash)
    assert k.limit_microdollars and k.limit_daily_microdollars and k.limit_monthly_microdollars
    # Now save with all budget fields blank -> everything back to undefined.
    client.post(
        f"/console/api-keys/{key.hash}/limit",
        data={"limit": "", "limit_daily": "", "limit_weekly": "", "limit_monthly": ""},
        follow_redirects=False,
    )
    k = STORE.get_key_by_hash(key.hash)
    assert k.limit_microdollars is None
    assert k.limit_daily_microdollars is None
    assert k.limit_weekly_microdollars is None
    assert k.limit_monthly_microdollars is None


def test_budget_inputs_have_no_number_spinners(console: dict) -> None:
    """The USD budget inputs are text/decimal — no up/down microdollar spinners."""
    page = console["client"].get("/console/api-keys").text
    assert 'type="number"' not in page  # spinners removed
    assert 'inputmode="decimal"' in page


def test_existing_keys_are_primary_when_workspace_has_keys(console: dict) -> None:
    """Existing keys stay primary with creation and budget controls exposed."""
    page = console["client"].get("/console/api-keys").text

    assert page.index("Existing keys") < page.index("Create a new API key")
    assert "data-rewards-key-card" not in page
    assert 'data-action="open-new-key"' in page
    assert '<section class="panel key-create-panel" id="new-api-key">' in page
    assert '<details class="key-limit-editor">' not in page
    assert 'class="key-budget-form"' in page
    assert '<summary>Help</summary>' in page
    assert 'href="/support">Support</a>' in page
    assert 'href="/support?category=feature#support-inquiry">Feature request</a>' in page
