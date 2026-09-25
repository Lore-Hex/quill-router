"""The lock-order guard must catch an inversion, not merely fail to find one.

A guard that records nothing passes every test in the suite, so each check here
pairs "the real path is clean" with "a deliberate inversion is rejected".
"""

from __future__ import annotations

import contextlib

import pytest

from tests.fakes import lock_order
from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router.config import Settings
from trusted_router.storage import configure_store


def _pg_store():
    store = postgres_store_on(sqlite_postgres_conn(check_same_thread=False))
    store.trust_settings = Settings(environment="test")
    return store


def test_guard_is_installed_and_fails_closed_if_a_hook_point_moves() -> None:
    lock_order.install()
    assert lock_order.recorder.installed
    from tests.fakes import postgres as pg_fake
    from tests.fakes import spanner as spanner_fake
    from trusted_router import storage_postgres

    # The names install() patches. If a refactor renames one, install() raises
    # rather than leaving a guard that silently observes nothing.
    assert hasattr(spanner_fake._FakeTransaction, "execute_update")
    assert hasattr(spanner_fake._FakeTransaction, "execute_sql")
    assert hasattr(storage_postgres.PostgresStore, "_run_transaction")
    assert hasattr(pg_fake.SqlitePostgresConn, "execute")


def test_detector_rejects_a_credit_lock_taken_after_the_key_lock() -> None:
    lock_order.recorder.reset()
    lock_order.recorder.record("t", "UPDATE tr_credit_balance SET reserved = 1")
    lock_order.recorder.record("t", "UPDATE tr_key_limit SET reserved = 1")
    lock_order.recorder.record("t", "UPDATE tr_credit_balance SET reserved = 2")
    with pytest.raises(lock_order.LockOrderError, match="deadlock shape"):
        lock_order.recorder.check("positive-control")
    lock_order.recorder.reset()


def test_detector_accepts_the_shipped_order() -> None:
    lock_order.recorder.reset()
    lock_order.recorder.record("t", "UPDATE tr_credit_balance SET reserved = 1")
    lock_order.recorder.record("t", "SELECT pause_epoch FROM tr_credit_balance FOR UPDATE")
    lock_order.recorder.record("t", "UPDATE tr_key_limit SET reserved = 1")
    lock_order.recorder.check("negative-control")
    lock_order.recorder.reset()


def test_postgres_hook_catches_an_inverted_real_transaction() -> None:
    """End to end: the Postgres hook, not just the detector, sees the order."""
    store = _pg_store()
    configure_store(store)
    lock_order.recorder.reset()

    def inverted(conn):
        conn.execute("UPDATE tr_key_limit SET reserved = reserved WHERE key_hash = %s", ("k",))
        conn.execute(
            "UPDATE tr_credit_balance SET reserved = reserved WHERE workspace_id = %s", ("w",),
        )

    store._run_transaction(inverted)
    with pytest.raises(lock_order.LockOrderError):
        lock_order.recorder.check("postgres-hook-control")
    lock_order.recorder.reset()


def test_postgres_hook_accepts_the_shipped_order() -> None:
    store = _pg_store()
    configure_store(store)
    lock_order.recorder.reset()

    def shipped(conn):
        conn.execute(
            "UPDATE tr_credit_balance SET reserved = reserved WHERE workspace_id = %s", ("w",),
        )
        conn.execute("UPDATE tr_key_limit SET reserved = reserved WHERE key_hash = %s", ("k",))

    store._run_transaction(shipped)
    lock_order.recorder.check("postgres-hook-clean")
    assert lock_order.recorder.both_tables_seen >= 1
    lock_order.recorder.reset()


def test_a_real_gateway_authorize_exercises_both_tables() -> None:
    """Non-vacuity: the guard observes the production path, not an empty trace.

    Without this a broken hook would record nothing and every lock-order check
    in the suite would pass by observing no transactions at all.
    """
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn

    store = postgres_store_on(sqlite_postgres_conn(check_same_thread=False))
    store.trust_settings = Settings(environment="test")
    configure_store(store)
    workspace = store.create_workspace("w", "w", trial_credit_microdollars=5_000_000)
    _, key = store.create_api_key(
        workspace_id=workspace.id, name="k", creator_user_id="u",
        limit_microdollars=5_000_000,
    )
    lock_order.recorder.reset()
    store.reserve_key_limit(key.hash, 100, usage_type="Credits")
    store.reserve(workspace.id, key.hash, 100, idempotency_key="idem")
    lock_order.recorder.violations()
    assert lock_order.recorder.both_tables_seen >= 1, (
        "the guard saw no transaction touching both credit and key rows; "
        "the hooks are not observing the production path"
    )
    lock_order.recorder.check("non-vacuity")
    lock_order.recorder.reset()

def test_spanner_hook_catches_an_inverted_transaction_nobody_opted_in() -> None:
    """The Spanner hook is universal: no test has to hand the transaction over.

    tests/fakes/spanner_order.credit_before_key only inspects transactions a
    test passes to it, so a transaction nobody wrote an assertion for was
    unprotected. Here the statements are issued straight at the transaction
    object and the guard still objects.

    Whether the fake can execute these statements against an unseeded database
    is beside the point -- the guard records the ORDER of the calls, which has
    already happened by the time the fake decides it cannot run them.
    """
    from tests.fakes.spanner import FakeSpannerDatabase, _FakeTransaction

    transaction = _FakeTransaction(FakeSpannerDatabase())
    lock_order.recorder.reset()
    with contextlib.suppress(Exception):
        transaction.execute_sql(
            "SELECT reserved FROM tr_key_limit WHERE key_hash=@kh",
            params={"kh": "k"}, param_types={},
        )
    with contextlib.suppress(Exception):
        transaction.execute_update(
            "UPDATE tr_credit_balance SET reserved=1 WHERE workspace_id=@ws",
            params={"ws": "w"}, param_types={},
        )
    with pytest.raises(lock_order.LockOrderError):
        lock_order.recorder.check("spanner-hook-control")
    lock_order.recorder.reset()
