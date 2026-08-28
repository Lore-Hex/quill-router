from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.storage import STORE, Generation, InMemoryStore, OAuthApp
from trusted_router.types import UsageType

APP_ID = "budget-app"


def _grant(
    *,
    user_id: str = "alice@example.com",
    app_id: str = APP_ID,
    bps: int = 500,
) -> tuple[str, str, list[str]]:
    user = STORE.ensure_user(user_id)
    STORE.set_user_identity_status(user.id, status="approved", verified_name="Alice Owner")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    if STORE.get_oauth_app(app_id) is None:
        STORE.create_oauth_app(
            OAuthApp(
                id=app_id,
                owner_user_id=user.id,
                name="Budget App",
                redirect_uris=["https://app.example/callback"],
                logo_url="https://app.example/logo.png",
                markup_basis_points=bps,
            )
        )
    raw_keys: list[str] = []
    for index, scopes in enumerate((["inference"], ["profile", "balance:read"])):
        raw, key = STORE.create_api_key(
            workspace_id=workspace.id,
            name=f"delegated {index}",
            creator_user_id=user.id,
            scopes=scopes,
            app_id=app_id,
            limit_monthly_microdollars=20 * MICRODOLLARS_PER_DOLLAR,
        )
        raw_keys.append(raw)
        STORE.add_generation(
            Generation(
                id=f"gen-{app_id}-{index}",
                request_id=f"req-{app_id}-{index}",
                workspace_id=workspace.id,
                key_hash=key.hash,
                model="test/model",
                provider_name="test",
                app="test",
                tokens_prompt=1,
                tokens_completion=1,
                total_cost_microdollars=MICRODOLLARS_PER_DOLLAR,
                usage_type=UsageType.CREDITS,
                speed_tokens_per_second=1,
                finish_reason="stop",
                status="completed",
                streamed=False,
                app_id=app_id,
            )
        )
    return user.id, workspace.id, raw_keys


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_delegated_key_is_rejected_by_every_management_endpoint(
    client: TestClient,
    method: str,
) -> None:
    _user_id, _workspace_id, raw_keys = _grant()
    headers = {"authorization": f"Bearer {raw_keys[0]}"}
    path = "/v1/oauth/authorized-apps" + (f"/{APP_ID}" if method != "get" else "")
    response = getattr(client, method)(
        path,
        headers=headers,
        **({"json": {"monthly_budget": "100"}} if method == "patch" else {}),
    )
    assert response.status_code == 403
    assert "delegated" in response.text.lower()


def test_list_groups_keys_and_reports_budget_usage_scopes_and_disclosure(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _grant()
    row = client.get("/v1/oauth/authorized-apps", headers=user_headers).json()["data"][0]
    assert row["app_id"] == APP_ID
    assert row["key_count"] == 2
    assert row["scopes"] == ["balance:read", "inference", "profile"]
    assert row["budget"] == {
        "monthly_budget": "20",
        "limit_microdollars": 20 * MICRODOLLARS_PER_DOLLAR,
        "used_microdollars": 2 * MICRODOLLARS_PER_DOLLAR,
        "reset_window": "monthly",
    }
    assert row["owner_verified_legal_name"] == "Alice Owner"
    assert row["markup_basis_points"] == 500
    assert "adds 5%" in row["markup_disclosure"]
    assert "key" not in row


def test_list_reports_no_field_that_only_one_backend_can_populate(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    """Every listed field must be populated by the real backends.

    A `last_used_at` derived from generation timestamps was dropped: only
    InMemoryStore could supply it, so Spanner and Postgres would have shown
    a permanently-null field, and the test that "covered" it reached into
    STORE.generation_store directly -- proving the in-memory path only.
    Recency is already conveyed by the usage windows, which every backend
    populates from the typed tr_key_limit shards.
    """
    _grant()

    row = client.get("/v1/oauth/authorized-apps", headers=user_headers).json()["data"][0]

    assert "last_used_at" not in row
    assert row["budget"]["used_microdollars"] > 0


def test_zero_markup_has_no_disclosure(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _grant(app_id="zero-markup", bps=0)
    rows = client.get("/v1/oauth/authorized-apps", headers=user_headers).json()["data"]
    row = next(item for item in rows if item["app_id"] == "zero-markup")
    assert row["markup_basis_points"] == 0
    assert row["markup_disclosure"] is None


def test_patch_updates_every_live_key_and_rejects_bad_values(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _user_id, workspace_id, _raw_keys = _grant()
    changed = client.patch(
        f"/v1/oauth/authorized-apps/{APP_ID}",
        headers=user_headers,
        json={"monthly_budget": "42.50"},
    )
    assert changed.status_code == 200, changed.text
    keys = [key for key in STORE.list_keys(workspace_id) if key.app_id == APP_ID]
    assert {key.limit_monthly_microdollars for key in keys} == {42_500_000}
    for bad in ("junk", "-1"):
        response = client.patch(
            f"/v1/oauth/authorized-apps/{APP_ID}",
            headers=user_headers,
            json={"monthly_budget": bad},
        )
        assert response.status_code == 400


def test_patch_repairs_a_write_that_persists_then_raises_and_can_retry(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _user_id, workspace_id, _raw_keys = _grant()
    raw, _key = STORE.create_api_key(
        workspace_id=workspace_id,
        name="delegated 3",
        creator_user_id=_user_id,
        scopes=["inference"],
        app_id=APP_ID,
        limit_monthly_microdollars=20 * MICRODOLLARS_PER_DOLLAR,
    )
    assert raw
    original = InMemoryStore.update_key
    calls = 0

    def persist_then_raise(self, key_hash, patch):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        result = original(self, key_hash, patch)
        if calls == 2:
            raise RuntimeError("injected post-persist failure")
        return result

    monkeypatch.setattr(InMemoryStore, "update_key", persist_then_raise)
    failed = client.patch(
        f"/v1/oauth/authorized-apps/{APP_ID}",
        headers=user_headers,
        json={"monthly_budget": "42"},
    )
    assert failed.status_code == 500
    keys = [key for key in STORE.list_keys(workspace_id) if key.app_id == APP_ID]
    assert len(keys) == 3
    assert {key.limit_monthly_microdollars for key in keys} == {
        20 * MICRODOLLARS_PER_DOLLAR
    }
    monkeypatch.setattr(InMemoryStore, "update_key", original)
    retried = client.patch(
        f"/v1/oauth/authorized-apps/{APP_ID}",
        headers=user_headers,
        json={"monthly_budget": "42"},
    )
    assert retried.status_code == 200, retried.text
    assert {
        key.limit_monthly_microdollars
        for key in STORE.list_keys(workspace_id)
        if key.app_id == APP_ID
    } == {42 * MICRODOLLARS_PER_DOLLAR}


def test_other_workspace_is_not_listed_or_mutable(
    client: TestClient,
) -> None:
    _grant()
    bob_headers = {"x-trustedrouter-user": "bob@example.com"}
    assert client.get("/v1/oauth/authorized-apps", headers=bob_headers).json()["data"] == []
    assert client.patch(
        f"/v1/oauth/authorized-apps/{APP_ID}",
        headers=bob_headers,
        json={"monthly_budget": "99"},
    ).status_code == 404
    assert client.delete(
        f"/v1/oauth/authorized-apps/{APP_ID}", headers=bob_headers
    ).status_code == 404


def test_delete_disables_all_keys_gateway_denies_and_second_delete_succeeds(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    _user_id, workspace_id, raw_keys = _grant()
    first = client.delete(f"/v1/oauth/authorized-apps/{APP_ID}", headers=user_headers)
    second = client.delete(f"/v1/oauth/authorized-apps/{APP_ID}", headers=user_headers)
    assert first.status_code == second.status_code == 200
    keys = [key for key in STORE.list_keys(workspace_id) if key.app_id == APP_ID]
    assert len(keys) == 2 and all(key.disabled for key in keys)
    key = STORE.get_key_by_raw(raw_keys[0])
    assert key is not None
    denied = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "idempotency_key": "revoked-oauth-app",
        },
    )
    # Regression proof: deleting gateway.py's api_key.disabled check turns
    # this assertion red; the test intentionally drives the real gateway.
    assert denied.status_code == 401
    assert len(STORE.list_keys(workspace_id)) == 2  # activity linkage remains intact
    assert STORE.get_generation(f"gen-{APP_ID}-0") is not None


def test_backend_without_key_writes_reports_why_not_a_phantom_repair_failure(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgresStore.update_key is still increment-1 unimplemented.

    Without this branch the NotImplementedError falls into the repair path,
    which calls update_key again, raises again, and escalates a critical
    "keys disagree" alert -- for a backend that never wrote anything at all.
    The operator gets a data-integrity page for a capability gap.
    """
    _grant()

    def unimplemented(*_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("PostgresStore.update_key is not implemented in increment 1")

    monkeypatch.setattr(InMemoryStore, "update_key", unimplemented)
    response = client.patch(
        f"/v1/oauth/authorized-apps/{APP_ID}",
        headers=user_headers,
        json={"monthly_budget": "50"},
    )

    assert response.status_code == 501
    assert response.json()["error"]["type"] == "endpoint_not_supported"


def test_not_implemented_after_a_successful_write_repairs_instead_of_501(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability gap is only a capability gap before anything is written.

    The first version of the 501 branch wrapped the whole mutation, so a
    NotImplementedError raised after a write had already landed returned 501
    and skipped repair -- misreporting a genuine failure AND leaving the
    app's keys disagreeing. Only the first write, proven not to have
    persisted, may be treated as an unsupported backend.
    """
    _user_id, workspace_id, _raw = _grant()
    original = InMemoryStore.update_key
    calls = {"n": 0}

    def fail_after_first(self: InMemoryStore, key_hash: str, patch: dict[str, object]):
        calls["n"] += 1
        if calls["n"] == 2:
            raise NotImplementedError("simulated late capability failure")
        return original(self, key_hash, patch)

    monkeypatch.setattr(InMemoryStore, "update_key", fail_after_first)
    response = client.patch(
        f"/v1/oauth/authorized-apps/{APP_ID}",
        headers=user_headers,
        json={"monthly_budget": "50"},
    )
    monkeypatch.setattr(InMemoryStore, "update_key", original)

    assert response.status_code == 500
    assert response.json()["error"]["type"] != "endpoint_not_supported"
    budgets = {
        key.limit_monthly_microdollars
        for key in STORE.list_keys(workspace_id)
        if key.app_id == APP_ID
    }
    assert len(budgets) == 1, f"keys left disagreeing: {budgets}"


def test_revoke_also_reports_an_unwritable_backend(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE shares the helper, so it must report the gap the same way."""
    _grant()

    def unimplemented(*_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("PostgresStore.update_key is not implemented in increment 1")

    monkeypatch.setattr(InMemoryStore, "update_key", unimplemented)
    response = client.delete(
        f"/v1/oauth/authorized-apps/{APP_ID}", headers=user_headers
    )

    assert response.status_code == 501
    assert response.json()["error"]["type"] == "endpoint_not_supported"
