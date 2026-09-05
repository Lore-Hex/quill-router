"""Postgres API-key patch transaction regressions."""

from __future__ import annotations

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn


def _key_store():
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    _raw, key = store.create_api_key(
        workspace_id="ws-update-key",
        name="before",
        creator_user_id=None,
        limit_microdollars=10,
        limit_daily_microdollars=20,
    )
    return store, conn, key


def _assert_second_shard_cap_patch_is_rejected(store, conn, key) -> None:
    conn.execute(
        "INSERT INTO tr_key_limit"
        " (workspace_id, key_hash, shard, limit_micro, include_byok, day_limit_micro)"
        " VALUES (%s, %s, 1, 7, TRUE, 8)",
        (key.workspace_id, key.hash),
    )
    before = conn.execute(
        "SELECT shard, limit_micro, include_byok, day_limit_micro"
        " FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()

    with pytest.raises(RuntimeError):
        store.update_key(key.hash, {"limit_microdollars": 101})

    after = conn.execute(
        "SELECT shard, limit_micro, include_byok, day_limit_micro"
        " FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()
    assert after == before


def test_postgres_update_key_declares_write_capability() -> None:
    store, _conn, _key = _key_store()
    assert store.supports_key_writes() is True


def test_postgres_metadata_patch_does_not_write_typed_limit() -> None:
    store, conn, key = _key_store()
    before = conn.execute(
        "SELECT shard, limit_micro, day_limit_micro"
        " FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()
    conn.fail_on = "tr_key_limit"

    updated = store.update_key(key.hash, {"name": "after", "disabled": True})

    assert updated is not None
    assert updated.name == "after"
    assert updated.disabled is True
    conn.fail_on = None
    after = conn.execute(
        "SELECT shard, limit_micro, day_limit_micro"
        " FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()
    assert after == before
    # The metadata behavior predates the guard, so retain an in-test mutation
    # tripwire proving this test also fails if the cap guard is removed.
    _assert_second_shard_cap_patch_is_rejected(store, conn, key)


def test_postgres_cap_patch_updates_single_shard_zero() -> None:
    store, conn, key = _key_store()

    updated = store.update_key(
        key.hash,
        {
            "limit_microdollars": 99,
            "limit_daily_microdollars": 30,
            "include_byok_in_limit": False,
        },
    )

    assert updated is not None
    assert updated.limit_microdollars == 99
    rows = conn.execute(
        "SELECT shard, limit_micro, include_byok, day_limit_micro"
        " FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()
    assert rows == [(0, 99, 0, 30)]
    # The successful one-shard behavior predates the guard; this negative
    # control makes the test independently sensitive to removing that guard.
    _assert_second_shard_cap_patch_is_rejected(store, conn, key)


def test_postgres_cap_patch_rejects_multiple_shards_without_changes() -> None:
    store, conn, key = _key_store()
    conn.execute(
        "INSERT INTO tr_key_limit"
        " (workspace_id, key_hash, shard, limit_micro, include_byok, day_limit_micro)"
        " VALUES (%s, %s, 1, 7, TRUE, 8)",
        (key.workspace_id, key.hash),
    )
    before = conn.execute(
        "SELECT shard, limit_micro, include_byok, day_limit_micro"
        " FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()

    with pytest.raises(RuntimeError) as exc_info:
        store.update_key(
            key.hash,
            {
                "name": "must-roll-back",
                "limit_microdollars": 99,
                "limit_daily_microdollars": 30,
                "include_byok_in_limit": False,
            },
        )

    message = str(exc_info.value)
    assert f"key_hash={key.hash}" in message
    assert "shard_count=2" in message
    assert "distributing a cap across shards is not implemented on this backend" in message
    after = conn.execute(
        "SELECT shard, limit_micro, include_byok, day_limit_micro"
        " FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()
    assert after == before
    stored = store.get_key_by_hash(key.hash)
    assert stored is not None
    assert stored.name == "before"
    assert stored.limit_microdollars == 10


def test_postgres_cap_and_entity_writes_roll_back_together() -> None:
    store, conn, key = _key_store()
    conn.fail_on = "UPDATE tr_key_limit"

    with pytest.raises(RuntimeError, match="connection reset"):
        store.update_key(key.hash, {"name": "must-roll-back", "limit_microdollars": 99})

    conn.fail_on = None
    stored = store.get_key_by_hash(key.hash)
    assert stored is not None
    assert stored.name == "before"
    assert stored.limit_microdollars == 10
