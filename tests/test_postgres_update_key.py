"""Postgres API-key patch transaction regressions."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router.spend_windows import KeyLimitExceeded, KeyWindowLimitExceeded


def _key_store(
    *,
    limit_microdollars: int | None = 10,
    limit_daily_microdollars: int | None = 20,
    include_byok_in_limit: bool = True,
    budget_alert_only: bool = False,
):
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    _raw, key = store.create_api_key(
        workspace_id="ws-update-key",
        name="before",
        creator_user_id=None,
        limit_microdollars=limit_microdollars,
        limit_daily_microdollars=limit_daily_microdollars,
        include_byok_in_limit=include_byok_in_limit,
        budget_alert_only=budget_alert_only,
    )
    return store, conn, key


def _reserved(conn: Any, key_hash: str) -> int:
    row = conn.execute(
        "SELECT reserved FROM tr_key_limit WHERE key_hash = %s AND shard = 0",
        (key_hash,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _authorization(
    store: Any,
    key: Any,
    *,
    usage_type: str,
    estimated_microdollars: int,
    key_reserved_microdollars: int,
):
    return store.create_gateway_authorization(
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        model_id="test/model",
        provider="test-provider",
        usage_type=usage_type,
        estimated_microdollars=estimated_microdollars,
        credit_reservation_id=None,
        key_reserved_microdollars=key_reserved_microdollars,
    )


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


def test_postgres_cap_patch_rejects_single_nonzero_shard_without_changes() -> None:
    """One typed row is insufficient: the only supported layout is shard 0."""
    store, conn, key = _key_store()
    conn.execute(
        "DELETE FROM tr_key_limit WHERE workspace_id = %s AND key_hash = %s",
        (key.workspace_id, key.hash),
    )
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
        store.update_key(key.hash, {"name": "must-roll-back", "limit_microdollars": 99})

    message = str(exc_info.value)
    assert "shard_count=1" in message
    assert "shards=[1]" in message
    assert "exactly one row at shard 0 is required" in message
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


def test_postgres_cap_patch_rejects_zero_row_guarded_update_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row lost after the layout read must not become a false success."""
    store, conn, key = _key_store()
    original_execute = conn.execute

    def execute_after_row_vanishes(
        sql: str,
        params: tuple[Any, ...] = (),
        **kwargs: Any,
    ) -> Any:
        if sql.startswith("UPDATE tr_key_limit SET") and " limit_micro = %s" in sql:
            original_execute(
                "DELETE FROM tr_key_limit WHERE workspace_id = %s AND key_hash = %s",
                (key.workspace_id, key.hash),
            )
        return original_execute(sql, params, **kwargs)

    monkeypatch.setattr(conn, "execute", execute_after_row_vanishes)

    with pytest.raises(RuntimeError, match="cap UPDATE affected 0 rows"):
        store.update_key(key.hash, {"name": "must-roll-back", "limit_microdollars": 99})

    # Both changes happened inside the failed transaction. Inspect the entity
    # first so disabling transactions specifically fails the JSON rollback check.
    stored = store.get_key_by_hash(key.hash)
    assert stored is not None
    assert stored.name == "before"
    assert stored.limit_microdollars == 10
    rows = conn.execute(
        "SELECT shard, limit_micro FROM tr_key_limit WHERE key_hash = %s ORDER BY shard",
        (key.hash,),
    ).fetchall()
    assert rows == [(0, 10)]


def test_postgres_cap_and_entity_writes_roll_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, conn, key = _key_store()
    original_execute = conn.execute
    observed_partial_write = False

    def fail_typed_write(sql: str, params: tuple[Any, ...] = (), **kwargs: Any) -> Any:
        nonlocal observed_partial_write
        if sql.startswith("UPDATE tr_key_limit SET"):
            row = original_execute(
                "SELECT body FROM tr_entities WHERE kind = %s AND id = %s",
                ("api_key", key.hash),
            ).fetchone()
            assert json.loads(row[0])["name"] == "must-roll-back"
            observed_partial_write = True
            raise RuntimeError("connection reset mid-transaction")
        return original_execute(sql, params, **kwargs)

    monkeypatch.setattr(conn, "execute", fail_typed_write)

    with pytest.raises(RuntimeError, match="connection reset"):
        store.update_key(key.hash, {"name": "must-roll-back", "limit_microdollars": 99})

    assert observed_partial_write
    stored = store.get_key_by_hash(key.hash)
    assert stored is not None
    assert stored.name == "before"
    assert stored.limit_microdollars == 10


def test_postgres_settle_uses_frozen_zero_hold_after_uncapped_key_becomes_capped() -> None:
    """Settling uncapped request A must not release capped request B's hold."""
    store, conn, key = _key_store(
        limit_microdollars=None,
        limit_daily_microdollars=None,
    )
    reservation_a = store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    assert reservation_a.reserved_microdollars == 0
    authorization_a = _authorization(
        store,
        key,
        usage_type="Credits",
        estimated_microdollars=100,
        key_reserved_microdollars=reservation_a.reserved_microdollars,
    )

    store.update_key(key.hash, {"limit_microdollars": 100})
    reservation_b = store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    assert reservation_b.reserved_microdollars == 100
    assert _reserved(conn, key.hash) == 100

    assert store.finalize_gateway_authorization(
        authorization_a.id,
        success=True,
        actual_microdollars=0,
        selected_usage_type="Credits",
    )
    assert _reserved(conn, key.hash) == 100
    with pytest.raises(KeyLimitExceeded):
        store.reserve_key_limit(key.hash, 1, usage_type="Credits")


def test_postgres_settle_does_not_release_new_hold_after_byok_becomes_included() -> None:
    """A BYOK-excluded request records zero even if the flag later flips."""
    store, conn, key = _key_store(
        limit_microdollars=100,
        limit_daily_microdollars=None,
        include_byok_in_limit=False,
    )
    reservation_a = store.reserve_key_limit(key.hash, 100, usage_type="BYOK")
    assert reservation_a.reserved_microdollars == 0
    authorization_a = _authorization(
        store,
        key,
        usage_type="BYOK",
        estimated_microdollars=100,
        key_reserved_microdollars=reservation_a.reserved_microdollars,
    )

    store.update_key(key.hash, {"include_byok_in_limit": True})
    reservation_b = store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    assert reservation_b.reserved_microdollars == 100

    assert store.finalize_gateway_authorization(
        authorization_a.id,
        success=False,
        actual_microdollars=0,
        selected_usage_type="BYOK",
    )
    assert _reserved(conn, key.hash) == 100
    with pytest.raises(KeyLimitExceeded):
        store.reserve_key_limit(key.hash, 1, usage_type="Credits")


def test_postgres_settle_releases_frozen_byok_hold_after_byok_becomes_excluded() -> None:
    """A BYOK hold already taken must release after the flag flips off."""
    store, conn, key = _key_store(
        limit_microdollars=100,
        limit_daily_microdollars=None,
        include_byok_in_limit=True,
    )
    reservation_a = store.reserve_key_limit(key.hash, 100, usage_type="BYOK")
    assert reservation_a.reserved_microdollars == 100
    authorization_a = _authorization(
        store,
        key,
        usage_type="BYOK",
        estimated_microdollars=100,
        key_reserved_microdollars=reservation_a.reserved_microdollars,
    )
    assert _reserved(conn, key.hash) == 100

    store.update_key(key.hash, {"include_byok_in_limit": False})
    assert store.finalize_gateway_authorization(
        authorization_a.id,
        success=False,
        actual_microdollars=0,
        selected_usage_type="BYOK",
    )
    assert _reserved(conn, key.hash) == 0
    reservation_c = store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    assert reservation_c.reserved_microdollars == 100


def test_postgres_alert_only_bypasses_window_block_but_keeps_lifetime_hold() -> None:
    store, conn, key = _key_store(
        limit_microdollars=100,
        limit_daily_microdollars=0,
        budget_alert_only=True,
    )

    reservation = store.reserve_key_limit(key.hash, 60, usage_type="Credits")

    assert reservation.window_decision is None
    assert reservation.reserved_microdollars == 60
    assert _reserved(conn, key.hash) == 60
    with pytest.raises(KeyLimitExceeded):
        store.reserve_key_limit(key.hash, 41, usage_type="Credits")

    # The zero daily cap remains authoritative when alert-only is disabled.
    store.update_key(key.hash, {"budget_alert_only": False})
    with pytest.raises(KeyWindowLimitExceeded):
        store.reserve_key_limit(key.hash, 1, usage_type="Credits")


@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("frozen", ["absent", 0])
def test_postgres_legacy_missing_hold_differs_from_frozen_zero(success, frozen) -> None:
    store, conn, key = _key_store(limit_microdollars=100, limit_daily_microdollars=None)
    store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    auth = _authorization(
        store, key, usage_type="Credits", estimated_microdollars=100,
        key_reserved_microdollars=0,
    )
    if frozen != 0:
        body = json.loads(conn.execute(
            "SELECT body FROM tr_entities WHERE kind = %s AND id = %s",
            ("gateway_authorization", auth.id),
        ).fetchone()[0])
        body.pop("key_reserved_microdollars")
        conn.execute(
            "UPDATE tr_entities SET body = %s WHERE kind = %s AND id = %s",
            (json.dumps(body), "gateway_authorization", auth.id),
        )
    assert store.finalize_gateway_authorization(
        auth.id, success=success, actual_microdollars=0, selected_usage_type="Credits",
    )
    assert _reserved(conn, key.hash) == (0 if frozen == "absent" else 100)


def _remove_frozen_hold(conn: Any, authorization_id: str) -> None:
    """Persist the pre-rollout shape, with the frozen field absent entirely."""
    body = json.loads(conn.execute(
        "SELECT body FROM tr_entities WHERE kind = %s AND id = %s",
        ("gateway_authorization", authorization_id),
    ).fetchone()[0])
    body.pop("key_reserved_microdollars")
    conn.execute(
        "UPDATE tr_entities SET body = %s WHERE kind = %s AND id = %s",
        (json.dumps(body), "gateway_authorization", authorization_id),
    )


def test_postgres_legacy_hold_releases_after_cap_removed_and_can_recap() -> None:
    store, conn, key = _key_store(limit_microdollars=100, limit_daily_microdollars=None)
    reservation = store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    assert reservation.reserved_microdollars == 100
    auth = _authorization(
        store, key, usage_type="Credits", estimated_microdollars=100,
        key_reserved_microdollars=100,
    )
    _remove_frozen_hold(conn, auth.id)
    store.update_key(key.hash, {"limit_microdollars": None})
    assert _reserved(conn, key.hash) == 100

    assert store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=0, selected_usage_type="Credits",
    )
    assert _reserved(conn, key.hash) == 0
    store.update_key(key.hash, {"limit_microdollars": 100})
    assert store.reserve_key_limit(key.hash, 1, usage_type="Credits").reserved_microdollars == 1


def test_postgres_legacy_cap_removed_byok_release_depends_only_on_inclusion() -> None:
    # Legacy behavior deliberately retains a BYOK hold if inclusion flips off.
    # Pair it with the included case: removing the cap alone must still release.
    for include_byok, expected_reserved in [(False, 100), (True, 0)]:
        store, conn, key = _key_store(limit_microdollars=100, limit_daily_microdollars=None)
        reservation = store.reserve_key_limit(key.hash, 100, usage_type="BYOK")
        assert reservation.reserved_microdollars == 100
        auth = _authorization(
            store, key, usage_type="BYOK", estimated_microdollars=100,
            key_reserved_microdollars=100,
        )
        _remove_frozen_hold(conn, auth.id)
        store.update_key(key.hash, {
            "limit_microdollars": None, "include_byok_in_limit": include_byok,
        })
        assert _reserved(conn, key.hash) == 100

        assert store.finalize_gateway_authorization(
            auth.id, success=True, actual_microdollars=0, selected_usage_type="BYOK",
        )
        assert _reserved(conn, key.hash) == expected_reserved


def test_postgres_cap_removed_frozen_zero_preserves_legacy_sibling_hold() -> None:
    store, conn, key = _key_store(limit_microdollars=None, limit_daily_microdollars=None)
    reservation = store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    assert reservation.reserved_microdollars == 0
    frozen = _authorization(
        store, key, usage_type="Credits", estimated_microdollars=100,
        key_reserved_microdollars=0,
    )
    store.update_key(key.hash, {"limit_microdollars": 100})
    assert store.reserve_key_limit(key.hash, 100, usage_type="Credits").reserved_microdollars == 100
    legacy = _authorization(
        store, key, usage_type="Credits", estimated_microdollars=100,
        key_reserved_microdollars=100,
    )
    _remove_frozen_hold(conn, legacy.id)
    store.update_key(key.hash, {"limit_microdollars": None})

    assert store.finalize_gateway_authorization(
        frozen.id, success=True, actual_microdollars=0, selected_usage_type="Credits",
    )
    assert _reserved(conn, key.hash) == 100
    # Identical estimates and current caps; only field presence differs.
    assert store.finalize_gateway_authorization(
        legacy.id, success=True, actual_microdollars=0, selected_usage_type="Credits",
    )
    assert _reserved(conn, key.hash) == 0


def test_postgres_gateway_authorize_freezes_uncapped_hold() -> None:
    from starlette.requests import Request

    from trusted_router.config import Settings
    from trusted_router.routes.internal import gateway
    from trusted_router.schemas import GatewayAuthorizeRequest
    from trusted_router.storage import CreditAccount, Workspace, configure_store

    store, conn, key = _key_store(limit_microdollars=None, limit_daily_microdollars=None)
    store._write_entity_tx(conn, "workspace", key.workspace_id, Workspace(
        id=key.workspace_id, name="test", owner_user_id="user-test",
    ))
    store._write_entity_tx(conn, "credit", key.workspace_id, CreditAccount(
        workspace_id=key.workspace_id,
    ))
    conn.execute(
        "INSERT INTO tr_credit_balance (workspace_id, shard, total_credits)"
        " VALUES (%s, 0, 1000000)", (key.workspace_id,),
    )
    configure_store(store)
    response = gateway._authorize_gateway_sync(
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        GatewayAuthorizeRequest(
            api_key_hash=key.hash, idempotency_key="frozen-hold-route",
            model="anthropic/claude-haiku-4.5",
            estimated_input_tokens=100, max_output_tokens=100,
        ),
        Settings(environment="test"),
    )
    auth_id = response["data"]["authorization_id"]
    auth = store.get_gateway_authorization(auth_id)
    store.update_key(key.hash, {"limit_microdollars": 100})
    store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    assert store.finalize_gateway_authorization(
        auth_id, success=True, actual_microdollars=0, selected_usage_type="Credits",
    )
    assert _reserved(conn, key.hash) == 100
    with pytest.raises(KeyLimitExceeded):
        store.reserve_key_limit(key.hash, 1, usage_type="Credits")
    assert auth.key_reserved_microdollars == 0
