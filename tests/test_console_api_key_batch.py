from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import microdollars_to_decimal
from trusted_router.routes.console.api_keys import _key_view
from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_codec import json_body
from trusted_router.storage_models import ApiKey, ApiKeyUsageSnapshot
from trusted_router.storage_postgres import PostgresStore


def _workspace(store: Any, email: str) -> tuple[Any, Any]:
    user = store.ensure_user(email)
    return user, store.list_workspaces_for_user(user.id)[0]


def _console_client(store: Any, email: str) -> tuple[TestClient, Any, Any]:
    user, workspace = _workspace(store, email)
    raw_session, _session = store.create_auth_session(
        user_id=user.id,
        provider="test",
        label=email,
        ttl_seconds=3600,
        workspace_id=workspace.id,
        state="active",
    )
    configure_store(store)
    client = TestClient(
        create_app(
            Settings(environment="test"),
            configure_store_arg=False,
            init_observability=False,
        )
    )
    client.cookies.set("tr_session", raw_session)
    return client, user, workspace


@pytest.mark.parametrize("key_count", [0, 1, 10])
def test_console_api_key_page_uses_two_read_rpcs_regardless_of_key_count(
    key_count: int,
) -> None:
    store, database, _bigtable = make_fake_store()
    client, user, workspace = _console_client(
        store,
        f"console-batch-{key_count}@example.com",
    )
    try:
        for index in range(key_count):
            store.create_api_key(
                workspace_id=workspace.id,
                name=f"key-{index}",
                creator_user_id=user.id,
            )
        before_reads = database.snapshot_execute_sql_calls
        before_snapshots = len(database.snapshot_calls)

        response = client.get("/console/api-keys")

        assert response.status_code == 200
        assert database.snapshot_execute_sql_calls - before_reads == 2
        assert database.snapshot_calls[before_snapshots:] == [{}, {}]
        assert "/* auth_session_context */" in database.snapshot_sql[-2]
        assert "/* console_api_keys */" in database.snapshot_sql[-1]
    finally:
        client.close()
        configure_store(InMemoryStore())


def test_spanner_bulk_key_usage_sums_only_complete_configured_shards() -> None:
    store, database, _bigtable = make_fake_store()
    user, workspace = _workspace(store, "console-shards@example.com")
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="sharded",
        creator_user_id=user.id,
        limit_daily_microdollars=10_000,
        limit_weekly_microdollars=20_000,
        limit_monthly_microdollars=30_000,
    )
    key.usage_shard_count = 3
    store._write_entity("api_key", key.hash, key)
    floors = window_floors(utcnow())
    base = database.typed["tr_key_limit"].pop((key.hash, 0))
    for shard in range(3):
        database.typed["tr_key_limit"][(key.hash, shard)] = {
            **base,
            "key_hash": key.hash,
            "shard": shard,
            "usage": 100 * (shard + 1),
            "byok_usage": 10 * (shard + 1),
            "reserved": shard + 1,
            "day_usage": shard + 1,
            "day_start": floors["daily"],
            "week_usage": 1000 * (shard + 1),
            "week_start": floors["weekly"] - dt.timedelta(seconds=1),
            "month_usage": 10 * (shard + 1),
            "month_start": floors["monthly"],
        }
    # A retired shard beyond the configured set is deliberately ignored, just
    # like the old per-key point read's shard<configured bound.
    database.typed["tr_key_limit"][(key.hash, 9)] = {
        **base,
        "key_hash": key.hash,
        "shard": 9,
        "usage": 999_999,
    }
    before = database.snapshot_execute_sql_calls

    snapshots = store.list_api_keys_with_usage(workspace.id)

    assert database.snapshot_execute_sql_calls - before == 1
    assert len(snapshots) == 1
    assert snapshots[0].api_key.hash == key.hash
    assert snapshots[0].usage_microdollars == 600
    assert snapshots[0].byok_usage_microdollars == 60
    assert snapshots[0].reserved_microdollars == 6
    assert snapshots[0].windows == {"daily": 6, "weekly": 0, "monthly": 60}


def test_spanner_bulk_key_usage_preserves_missing_row_fallback() -> None:
    store, database, _bigtable = make_fake_store()
    user, workspace = _workspace(store, "console-missing-typed@example.com")
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="legacy-json-counters",
        creator_user_id=user.id,
    )
    database.typed["tr_key_limit"].pop((key.hash, 0))
    key.usage_microdollars = 1234
    key.byok_usage_microdollars = 56
    key.reserved_microdollars = 7
    store._write_entity("api_key", key.hash, key)

    snapshots = store.list_api_keys_with_usage(workspace.id)

    assert len(snapshots) == 1
    assert snapshots[0].usage_microdollars == 1234
    assert snapshots[0].byok_usage_microdollars == 56
    assert snapshots[0].reserved_microdollars == 7
    assert snapshots[0].windows == {"daily": 0, "weekly": 0, "monthly": 0}


def test_spanner_bulk_projection_matches_the_legacy_fanout_values() -> None:
    """Differentially pin the old list+point-read result without concurrent writes."""
    store, database, _bigtable = make_fake_store()
    user, workspace = _workspace(store, "console-differential@example.com")
    keys: list[ApiKey] = []
    for index in range(3):
        _raw, key = store.create_api_key(
            workspace_id=workspace.id,
            name=f"differential-{index}",
            creator_user_id=user.id,
            limit_daily_microdollars=100_000,
        )
        key.created_at = f"2026-03-0{index + 1}T00:00:00Z"
        store._write_entity("api_key", key.hash, key)
        keys.append(key)

    # One legacy JSON-only key, one ordinary typed key, and one three-shard
    # key exercise all of the old route's result branches.
    legacy = keys[0]
    database.typed["tr_key_limit"].pop((legacy.hash, 0))
    legacy.usage_microdollars = 17
    store._write_entity("api_key", legacy.hash, legacy)

    ordinary = keys[1]
    database.typed["tr_key_limit"][(ordinary.hash, 0)]["usage"] = 23

    sharded = keys[2]
    sharded.usage_shard_count = 3
    store._write_entity("api_key", sharded.hash, sharded)
    base = database.typed["tr_key_limit"][(sharded.hash, 0)]
    for shard in range(3):
        database.typed["tr_key_limit"][(sharded.hash, shard)] = {
            **base,
            "key_hash": sharded.hash,
            "shard": shard,
            "usage": 100 + shard,
        }

    expected: list[ApiKeyUsageSnapshot] = []
    for key in store.list_keys(workspace.id):
        usage = store.typed_key_usage(key.hash, allow_stale=True)
        expected.append(
            ApiKeyUsageSnapshot(
                api_key=key,
                usage_microdollars=(
                    key.usage_microdollars if usage is None else int(usage["usage"])
                ),
                byok_usage_microdollars=(
                    key.byok_usage_microdollars
                    if usage is None
                    else int(usage["byok_usage"])
                ),
                reserved_microdollars=(
                    key.reserved_microdollars if usage is None else int(usage["reserved"])
                ),
                windows=(
                    {"daily": 0, "weekly": 0, "monthly": 0}
                    if usage is None
                    else dict(usage["windows"])
                ),
            )
        )

    assert store.list_api_keys_with_usage(workspace.id) == expected


def test_spanner_bulk_key_usage_fails_closed_on_incomplete_shards() -> None:
    store, database, _bigtable = make_fake_store()
    user, workspace = _workspace(store, "console-incomplete-shards@example.com")
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="incomplete",
        creator_user_id=user.id,
    )
    key.usage_shard_count = 3
    store._write_entity("api_key", key.hash, key)
    base = database.typed["tr_key_limit"][(key.hash, 0)]
    database.typed["tr_key_limit"][(key.hash, 2)] = {
        **base,
        "key_hash": key.hash,
        "shard": 2,
    }

    with pytest.raises(RuntimeError, match="usage shard set is incomplete"):
        store.list_api_keys_with_usage(workspace.id)


def test_spanner_bulk_key_list_rejects_dangling_noncanonical_and_foreign_rows() -> None:
    store, database, _bigtable = make_fake_store()
    user, workspace = _workspace(store, "console-owner@example.com")
    other_user, other_workspace = _workspace(store, "console-foreign@example.com")
    _raw, older = store.create_api_key(
        workspace_id=workspace.id,
        name="older",
        creator_user_id=user.id,
    )
    _raw, newer = store.create_api_key(
        workspace_id=workspace.id,
        name="newer",
        creator_user_id=user.id,
    )
    _raw, foreign = store.create_api_key(
        workspace_id=other_workspace.id,
        name="must-not-leak",
        creator_user_id=other_user.id,
    )
    _raw, corrupt = store.create_api_key(
        workspace_id=workspace.id,
        name="noncanonical-body-id",
        creator_user_id=user.id,
    )
    corrupt_record_id = corrupt.hash
    corrupt.hash = "body-does-not-match-row-id"
    store._write_entity("api_key", corrupt_record_id, corrupt)
    older.created_at = "2026-01-01T00:00:00Z"
    newer.created_at = "2026-02-01T00:00:00Z"
    store._write_entity("api_key", older.hash, older)
    store._write_entity("api_key", newer.hash, newer)
    store._write_entity(
        "api_key_by_workspace",
        f"{workspace.id}#dangling",
        {"key_id": "missing-key"},
    )
    store._write_entity(
        "api_key_by_workspace",
        f"{workspace.id}#alias",
        {"key_id": newer.hash},
    )
    store._write_entity(
        "api_key_by_workspace",
        f"{workspace.id}#{foreign.hash}",
        {"key_id": foreign.hash},
    )
    before = database.snapshot_execute_sql_calls

    snapshots = store.list_api_keys_with_usage(workspace.id)

    assert database.snapshot_execute_sql_calls - before == 1
    assert [snapshot.api_key.hash for snapshot in snapshots] == [newer.hash, older.hash]


def test_spanner_bulk_key_projection_is_strong_and_read_your_write() -> None:
    store, database, _bigtable = make_fake_store()
    user, workspace = _workspace(store, "console-immediate@example.com")
    snapshot_start = len(database.snapshot_calls)
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="created-now",
        creator_user_id=user.id,
    )

    assert [row.api_key.name for row in store.list_api_keys_with_usage(workspace.id)] == [
        "created-now"
    ]
    store.update_key(key.hash, {"name": "updated-now", "disabled": True})
    updated = store.list_api_keys_with_usage(workspace.id)
    assert [(row.api_key.name, row.api_key.disabled) for row in updated] == [
        ("updated-now", True)
    ]
    assert store.delete_key(key.hash) is True
    assert store.list_api_keys_with_usage(workspace.id) == []
    calls = database.snapshot_calls[snapshot_start:]
    assert len(calls) == 6
    assert calls == [{}] * len(calls)


def test_console_key_projection_keeps_the_existing_template_shape() -> None:
    key = ApiKey(
        hash="key-shape",
        salt="salt",
        secret_hash="digest",  # noqa: S106 - inert test fixture digest.
        lookup_hash="lookup",
        name="shape",
        label="sk-tr...shape",
        workspace_id="ws-shape",
        creator_user_id="user-shape",
        disabled=True,
        limit_microdollars=2_000_000,
        limit_daily_microdollars=500_000,
        budget_alert_only=True,
    )
    snapshot = ApiKeyUsageSnapshot(
        api_key=key,
        usage_microdollars=1_250_000,
        byok_usage_microdollars=9,
        reserved_microdollars=10,
        windows={"daily": 125_000, "weekly": 99, "monthly": 88},
    )

    assert _key_view(snapshot) == {
        "hash": "key-shape",
        "name": "shape",
        "label": "sk-tr...shape",
        "limit_display": "$2.00",
        "limit_input": microdollars_to_decimal(2_000_000),
        "usage_display": "$1.25",
        "windows": [
            {
                "name": "daily",
                "input": microdollars_to_decimal(500_000),
                "limit_display": "$0.50",
                "used_display": "$0.12",
            },
            {
                "name": "weekly",
                "input": "",
                "limit_display": None,
                "used_display": None,
            },
            {
                "name": "monthly",
                "input": "",
                "limit_display": None,
                "used_display": None,
            },
        ],
        "budget_alert_only": True,
        "disabled": True,
    }


def test_spanner_console_does_not_render_or_mutate_a_foreign_workspace_key() -> None:
    store, _database, _bigtable = make_fake_store()
    client, _user, workspace = _console_client(store, "console-security@example.com")
    other_user, other_workspace = _workspace(store, "console-security-other@example.com")
    _raw, own = store.create_api_key(
        workspace_id=workspace.id,
        name="visible-own-key",
        creator_user_id=_user.id,
    )
    _raw, foreign = store.create_api_key(
        workspace_id=other_workspace.id,
        name="foreign-canary-key",
        creator_user_id=other_user.id,
    )
    try:
        page = client.get("/console/api-keys")
        assert page.status_code == 200
        assert own.name in page.text
        assert foreign.name not in page.text
        limit = client.post(
            f"/console/api-keys/{foreign.hash}/limit",
            data={"limit": "99"},
            follow_redirects=False,
        )
        disable = client.post(
            f"/console/api-keys/{foreign.hash}/disable",
            follow_redirects=False,
        )
        assert limit.status_code == disable.status_code == 404
        assert store.get_key_by_hash(foreign.hash).disabled is False
        assert store.get_key_by_hash(foreign.hash).limit_microdollars is None
    finally:
        client.close()
        configure_store(InMemoryStore())


def test_postgres_bulk_key_projection_uses_one_portable_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ApiKey(
        hash="key-postgres",
        salt="salt",
        secret_hash="digest",  # noqa: S106 - inert test fixture digest.
        lookup_hash="lookup",
        name="postgres",
        label="sk-tr...postgres",
        workspace_id="ws-postgres",
        creator_user_id=None,
    )
    floor = window_floors(utcnow())["daily"]

    class Result:
        def fetchall(self) -> list[tuple[Any, ...]]:
            return [
                (
                    json_body(key),
                    0,
                    42,
                    3,
                    2,
                    7,
                    floor,
                    0,
                    None,
                    0,
                    None,
                )
            ]

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[str, ...]]] = []

        def execute(self, sql: str, params: tuple[str, ...]) -> Result:
            self.calls.append((sql, params))
            return Result()

    connection = Connection()
    monkeypatch.setattr(
        PostgresStore,
        "_run_transaction",
        lambda _self, operation: operation(connection),
    )
    store = PostgresStore.__new__(PostgresStore)

    snapshots = store.list_api_keys_with_usage("ws-postgres")

    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert "/* console_api_keys */" in sql
    assert "key_index.id = (%s || '#' || key_record.id)" in sql
    assert "key_record.body ->> 'workspace_id' = %s" in sql
    assert "key_record.body ->> 'hash' = key_record.id" in sql
    assert params == ("ws-postgres", "ws-postgres", "ws-postgres")
    assert len(snapshots) == 1
    assert snapshots[0].usage_microdollars == 42
    assert snapshots[0].windows["daily"] == 7
