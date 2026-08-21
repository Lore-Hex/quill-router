"""Postgres browser-key issuance executes atomically on the real SQL shape."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.fakes.postgres import (
    SqlitePostgresConn,
    postgres_store_on,
    sqlite_postgres_conn,
)
from trusted_router.storage_models import ApiKey, Workspace
from trusted_router.storage_postgres import PostgresStore


def _store() -> tuple[PostgresStore, SqlitePostgresConn, Workspace]:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = Workspace(id="ws-chat-browser", name="Chat", owner_user_id="user-chat")
    store._run_transaction(  # noqa: SLF001 - backend SQL harness.
        lambda transaction: store._write_entity_tx(  # noqa: SLF001
            transaction,
            "workspace",
            workspace.id,
            workspace,
        )
    )
    return store, conn, workspace


def _issue(store: PostgresStore, workspace: Workspace) -> tuple[str, ApiKey] | None:
    return store.issue_chat_browser_key(
        workspace_id=workspace.id,
        name="chat-browser-postgres",
        creator_user_id=workspace.owner_user_id,
        limit_microdollars=5_000_000,
        expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(days=30)).isoformat(),
        active_key_cap=1,
    )


def test_postgres_chat_browser_cap_refusal_writes_nothing() -> None:
    store, conn, workspace = _store()
    first = _issue(store, workspace)
    assert first is not None
    raw, key = first
    assert store.get_key_by_raw(raw) is not None
    assert key.management is False
    before = {
        kind: conn.count_entities(kind)
        for kind in ("api_key", "api_key_lookup", "api_key_by_workspace")
    }

    refused = _issue(store, workspace)

    assert refused is None
    assert {
        kind: conn.count_entities(kind)
        for kind in ("api_key", "api_key_lookup", "api_key_by_workspace")
    } == before


def test_postgres_chat_browser_partial_create_rolls_back() -> None:
    store, conn, workspace = _store()
    conn.fail_on = "INSERT INTO tr_key_limit"

    with pytest.raises(RuntimeError, match="connection reset"):
        _issue(store, workspace)

    assert conn.count_entities("api_key") == 0
    assert conn.count_entities("api_key_lookup") == 0
    assert conn.count_entities("api_key_by_workspace") == 0
