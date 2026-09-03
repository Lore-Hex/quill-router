"""PostgresStore._run_transaction's retry policy, pinned by SQLSTATE.

This is the one piece of the Postgres backend that every server-backed test
runs THROUGH but none ran ON: `tests/fakes/postgres.py` replaces
`_run_transaction` wholesale, so the retry loop had no coverage at all. It was
broken the entire time.

`except psycopg.errors.TransactionRollback` reads like it covers
serialization failures and deadlocks -- its comment said so -- but psycopg
generates one flat class per SQLSTATE, each deriving straight from its DB-API
base. `SerializationFailure` (40001) and `DeadlockDetected` (40P01) are
SIBLINGS of `TransactionRollback` (40000), not subclasses. The clause caught
only a bare 40000, so every real abort fell through to the generic handler and
reached the caller as `StoreUnavailable`.

Concretely, that turned a routine contended reserve into "Postgres could not
service the storage operation" instead of a retry that reports "insufficient
credits" -- observed on the Spanner PG emulator in CI run 31996299784, and on
Aurora DSQL it would be EVERY optimistic-concurrency abort, since 40001 is how
DSQL reports all of them.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from trusted_router.storage_errors import StoreConflict, StoreUnavailable
from trusted_router.storage_postgres import (
    _RETRYABLE_ROLLBACK_SQLSTATES,
    PostgresStore,
    _is_retryable_pgadapter_internal_error,
)


class _FakeTransaction:
    def __enter__(self) -> _FakeTransaction:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _FakeConnection:
    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


class _FakePool:
    def __init__(self) -> None:
        self.connections_handed_out = 0

    @contextmanager
    def connection(self) -> Any:
        self.connections_handed_out += 1
        yield _FakeConnection()


def _store(attempts: int = 8) -> tuple[PostgresStore, _FakePool]:
    """A PostgresStore with the real `_run_transaction` and no server.

    Built with `object.__new__` on purpose: `__init__` opens a connection pool,
    and the behaviour under test is the retry policy, not connection setup.
    """
    store = object.__new__(PostgresStore)
    pool = _FakePool()
    store._transaction_attempts = attempts
    store._pool = pool  # type: ignore[assignment]
    return store, pool


def _raise_then_succeed(error: BaseException, failures: int) -> Any:
    calls = {"n": 0}

    def operation(_conn: Any) -> str:
        calls["n"] += 1
        if calls["n"] <= failures:
            raise error
        return "committed"

    operation.calls = calls  # type: ignore[attr-defined]
    return operation


@pytest.mark.parametrize(
    ("sqlstate", "error"),
    [
        ("40001", psycopg.errors.SerializationFailure("aborted by concurrent write")),
        ("40P01", psycopg.errors.DeadlockDetected("deadlock detected")),
        ("40000", psycopg.errors.TransactionRollback("rolled back")),
    ],
)
def test_rolled_back_transactions_are_replayed(
    sqlstate: str, error: psycopg.Error
) -> None:
    """The whole point: an abort is retried, not handed to the caller."""
    assert error.sqlstate == sqlstate
    store, pool = _store()
    operation = _raise_then_succeed(error, failures=2)

    assert store._run_transaction(operation) == "committed"
    assert operation.calls["n"] == 3
    # A fresh connection per attempt -- a retry on the same aborted connection
    # would fail identically forever.
    assert pool.connections_handed_out == 3


def test_pgadapter_internal_parameter_error_is_replayed() -> None:
    """PGAdapter's analyzer abort is transient and the transaction is gone."""

    error = psycopg.errors.RaiseException("Index 2 out of bounds for length 2")
    assert error.sqlstate == "P0001"
    assert _is_retryable_pgadapter_internal_error(error)
    store, pool = _store()
    operation = _raise_then_succeed(error, failures=2)

    assert store._run_transaction(operation) == "committed"
    assert operation.calls["n"] == 3
    assert pool.connections_handed_out == 3


@pytest.mark.parametrize(
    "message",
    [
        "Index 2 out of bounds for length two",
        "business rule rejected the transfer",
        "Index 2 out of bounds for length 2; retry me",
    ],
)
def test_other_raise_exceptions_are_not_replayed(message: str) -> None:
    """A P0001 is not retryable unless it is the exact PGAdapter defect."""

    error = psycopg.errors.RaiseException(message)
    assert not _is_retryable_pgadapter_internal_error(error)
    store, pool = _store()
    operation = _raise_then_succeed(error, failures=1)

    with pytest.raises(StoreUnavailable):
        store._run_transaction(operation)
    assert operation.calls["n"] == 1
    assert pool.connections_handed_out == 1


@pytest.mark.parametrize(
    ("label", "error"),
    [
        # Deterministic: replaying it fails identically forever.
        ("40002", psycopg.errors.TransactionIntegrityConstraintViolation("bad")),
        # The statement may in fact have COMMITTED; replaying could double-apply.
        ("40003", psycopg.errors.StatementCompletionUnknown("unknown")),
    ],
)
def test_unsafe_class_40_states_are_not_replayed(
    label: str, error: psycopg.Error
) -> None:
    """Not every 40xxx is safe to replay, so the set is explicit, not a prefix."""
    assert error.sqlstate == label
    assert error.sqlstate not in _RETRYABLE_ROLLBACK_SQLSTATES
    store, pool = _store()
    operation = _raise_then_succeed(error, failures=1)

    with pytest.raises(StoreUnavailable):
        store._run_transaction(operation)
    assert operation.calls["n"] == 1
    assert pool.connections_handed_out == 1


def test_duplicate_key_is_a_conflict_not_a_retry() -> None:
    store, _pool = _store()
    operation = _raise_then_succeed(psycopg.errors.UniqueViolation("dup"), failures=1)

    with pytest.raises(StoreConflict):
        store._run_transaction(operation)
    assert operation.calls["n"] == 1


def test_exhausting_the_retry_budget_raises_conflict() -> None:
    store, _pool = _store(attempts=3)
    operation = _raise_then_succeed(
        psycopg.errors.SerializationFailure("aborted"), failures=99
    )

    with pytest.raises(StoreConflict) as exc:
        store._run_transaction(operation)
    assert operation.calls["n"] == 3
    # The original abort is preserved for the operator reading the traceback.
    assert isinstance(exc.value.__cause__, psycopg.errors.SerializationFailure)


def test_a_clean_run_does_not_retry() -> None:
    store, pool = _store()
    operation = _raise_then_succeed(RuntimeError("unused"), failures=0)

    assert store._run_transaction(operation) == "committed"
    assert operation.calls["n"] == 1
    assert pool.connections_handed_out == 1


def test_non_psycopg_errors_propagate_untouched() -> None:
    """A ValueError is the domain's own refusal (e.g. insufficient credits).

    It must reach the caller unwrapped and unretried -- swallowing it into a
    StoreError is what made a contended reserve look like an outage.
    """
    store, _pool = _store()
    operation = _raise_then_succeed(ValueError("insufficient credits"), failures=99)

    with pytest.raises(ValueError, match="insufficient credits"):
        store._run_transaction(operation)
    assert operation.calls["n"] == 1


def test_retryable_set_matches_what_psycopg_maps_those_states_to() -> None:
    """Pins the intent against a future psycopg reshuffling its class tree.

    If psycopg ever makes these classes nest, this still passes -- the store
    keys off SQLSTATE, which is the database's contract rather than the
    driver's. What must never happen is 40001 or 40P01 dropping out of the set.
    """
    assert psycopg.errors.SerializationFailure("x").sqlstate in _RETRYABLE_ROLLBACK_SQLSTATES
    assert psycopg.errors.DeadlockDetected("x").sqlstate in _RETRYABLE_ROLLBACK_SQLSTATES
    assert psycopg.errors.UniqueViolation("x").sqlstate not in _RETRYABLE_ROLLBACK_SQLSTATES
