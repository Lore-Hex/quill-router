from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, CreditMoney
from trusted_router.typed_balance import live_credit_summary


@pytest.fixture
def fake_spanner_client() -> Iterator[tuple[Any, Any, TestClient]]:
    store, db, _ = make_fake_store()
    configure_store(store)
    app = create_app(
        Settings(environment="local"),
        configure_store_arg=False,
        init_observability=False,
    )
    try:
        with TestClient(app) as client:
            yield store, db, client
    finally:
        configure_store(InMemoryStore())


def test_live_credit_summary_typed_row_wins() -> None:
    store, db, _ = make_fake_store()
    workspace_id = "ws_typed_summary"
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id),
    )
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace_id, 0)] = {
        "workspace_id": workspace_id,
        "shard": 0,
        "total_credits": 10_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)].update({
        "total_credits": 12_000_000,
        "total_usage": 7_000_000,
        "reserved": 1_000_000,
    })

    assert live_credit_summary(workspace_id, store=store) == {
        "total_credits": 12_000_000,
        "total_usage": 7_000_000,
        "reserved": 1_000_000,
        "available": 4_000_000,
    }


def test_live_credit_summary_typed_row_absent_fails_closed_despite_stale_json() -> None:
    store, _db, _ = make_fake_store()
    workspace_id = "ws_json_summary"
    store._write_entity(
        "credit",
        workspace_id,
        {
            "workspace_id": workspace_id,
            "total_credits_microdollars": 5_000_000,
            "total_usage_microdollars": 1_500_000,
            "reserved_microdollars": 500_000,
        },
    )

    with pytest.raises(RuntimeError, match="missing authoritative tr_credit_balance"):
        live_credit_summary(workspace_id, store=store)


def test_spanner_store_exposes_no_json_money_snapshot() -> None:
    store, _db, _ = make_fake_store()

    assert not hasattr(store, "credit_money_snapshot")


def test_live_credit_summary_no_credit_entity_returns_none() -> None:
    # A workspace with no 'credit' entity at all has no money to report.
    store, _db, _ = make_fake_store()
    assert live_credit_summary("ws_missing_entirely", store=store) is None


def test_live_credit_summary_memory_store_uses_single_book() -> None:
    store = InMemoryStore()
    workspace_id = "ws_memory_summary"
    store.credits[workspace_id] = CreditAccount(workspace_id=workspace_id)
    store.credit_money[workspace_id] = CreditMoney(
        total_credits_microdollars=2_000_000,
        total_usage_microdollars=1_500_000,
        reserved_microdollars=750_000,
    )

    assert live_credit_summary(workspace_id, store=store) == {
        "total_credits": 2_000_000,
        "total_usage": 1_500_000,
        "reserved": 750_000,
        "available": 0,
    }


def test_mcp_credits_get_returns_typed_numbers(fake_spanner_client: tuple[Any, Any, TestClient]) -> None:
    store, db, client = fake_spanner_client
    user = store.ensure_user("mcp-live-summary@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _seed_stale_json_live_typed(store, db, workspace.id)
    raw_key, _ = store.create_api_key(
        workspace_id=workspace.id,
        name="mcp",
        creator_user_id=user.id,
    )

    payload = _mcp_call(client, "credits-get", headers={"authorization": f"Bearer {raw_key}"})
    data = _tool_json(payload)["data"]

    assert data["workspace_id"] == workspace.id
    assert data["total_credits_microdollars"] == 10_000_000
    assert data["total_usage_microdollars"] == 7_000_000
    assert data["reserved_microdollars"] == 1_000_000
    assert data["available_microdollars"] == 2_000_000


def test_console_credits_page_renders_typed_money(fake_spanner_client: tuple[Any, Any, TestClient]) -> None:
    store, db, client = fake_spanner_client
    user = store.ensure_user("console-live-summary@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _seed_stale_json_live_typed(store, db, workspace.id)
    raw_token, _ = store.create_auth_session(
        user_id=user.id,
        provider="google",
        label="console-live-summary@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)

    response = client.get("/console/credits")

    assert response.status_code == 200, response.text[:300]
    assert '<div class="value">$2.00</div>' in response.text
    assert '<div class="value">$7.00</div>' in response.text
    assert '<div class="value">$8.50</div>' not in response.text
    assert '<div class="value">$1.00</div>' not in response.text


def _seed_stale_json_live_typed(store: Any, db: Any, workspace_id: str) -> None:
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id),
    )
    db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)].update({
        "total_credits": 10_000_000,
        "total_usage": 7_000_000,
        "reserved": 1_000_000,
    })


def _mcp_call(
    client: TestClient,
    name: str,
    arguments: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers=headers or {},
        json={
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tool_json(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload["result"]
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])
