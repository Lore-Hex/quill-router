"""Step 3 of the billing typed-column migration: conditional-DML enforcement.

These tests drive the conditional-DML reserve/release primitives against the
in-process Spanner fake, which models per-typed-row versioning so concurrent
``execute_update`` reservers serialize via conflict-abort (the fake analogue of
the real row write lock). The headline test proves the deadlock-fix property:
concurrent reservers on one hot workspace never overspend and resolve via
retry — no shared-read->exclusive-upgrade deadlock.

See docs/design/billing-typed-counters.md.
"""

from __future__ import annotations

import threading

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount
from trusted_router.storage_gcp_counter_dml import release_credit, reserve_credit
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _seed_credit(store, ws: str, total: int) -> None:
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=total)
    )


def _typed(db, ws: str) -> dict:
    return db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]


def _run_workers(workers: list[threading.Thread], barrier: threading.Barrier) -> None:
    for t in workers:
        t.start()
    try:
        barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        pass
    for t in workers:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in workers), "worker hang"


def test_conditional_reserve_accepts_until_exhausted() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_seq"
    _seed_credit(store, ws, 1_000_000)
    pt = store._param_types

    def reserve(amount: int) -> bool:
        return store._database.run_in_transaction(
            lambda t: reserve_credit(t, pt, ws, amount)
        )

    assert reserve(600_000) is True
    assert reserve(300_000) is True
    assert reserve(200_000) is False  # only 100k left
    assert reserve(100_000) is True
    assert _typed(db, ws)["reserved"] == 1_000_000


def test_conditional_reserve_no_overspend_under_concurrency() -> None:
    """The deadlock fix: N concurrent reservers on ONE hot workspace accept
    exactly floor(available/amount), never overspend, and resolve via
    conflict-retry (no deadlock)."""
    ws = "ws_hot"
    n = 8
    amount = 250_000  # 4 of 8 fit in 1_000_000
    barrier = threading.Barrier(n + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, ws, 1_000_000)
    pt = store._param_types

    results: list[bool] = []
    lock = threading.Lock()

    def reserve_once() -> None:
        ok = store._database.run_in_transaction(
            lambda t: reserve_credit(t, pt, ws, amount)
        )
        with lock:
            results.append(ok)

    _run_workers(
        [threading.Thread(target=reserve_once, daemon=True) for _ in range(n)], barrier
    )

    assert results.count(True) == 4, results
    assert results.count(False) == 4, results
    typed = _typed(db, ws)
    assert typed["reserved"] == 1_000_000  # exactly the balance, never more
    # Resolved by conflict-retry (the serialization), not deadlock.
    assert db.aborts >= n - 1, f"expected contention retries, got {db.aborts}"


def test_conditional_reserve_rejects_when_insufficient() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_poor"
    _seed_credit(store, ws, 100_000)
    pt = store._param_types
    ok = store._database.run_in_transaction(
        lambda t: reserve_credit(t, pt, ws, 250_000)
    )
    assert ok is False


def test_release_credit_settles_and_refunds() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_rel"
    _seed_credit(store, ws, 1_000_000)
    pt = store._param_types
    assert store._database.run_in_transaction(lambda t: reserve_credit(t, pt, ws, 500_000))

    # settle: release the 500k hold, book 480k actual
    count = store._database.run_in_transaction(
        lambda t: release_credit(t, pt, ws, 500_000, 480_000)
    )
    assert count == 1
    typed = _typed(db, ws)
    assert typed["reserved"] == 0
    assert typed["total_usage"] == 480_000

    # a second hold, then refund (actual=0): release hold, book nothing
    assert store._database.run_in_transaction(lambda t: reserve_credit(t, pt, ws, 200_000))
    refund_count = store._database.run_in_transaction(
        lambda t: release_credit(t, pt, ws, 200_000, 0)
    )
    assert refund_count == 1
    typed = _typed(db, ws)
    assert typed["reserved"] == 0
    assert typed["total_usage"] == 480_000  # unchanged by the refund


def test_release_underflow_is_noop_not_negative() -> None:
    """A stale/double release of more than `reserved` must be a 0-row no-op, not
    drive reserved negative (which would inflate apparent availability)."""
    store, db, _ = make_fake_store()
    ws = "ws_underflow"
    _seed_credit(store, ws, 1_000_000)
    pt = store._param_types
    assert store._database.run_in_transaction(lambda t: reserve_credit(t, pt, ws, 200_000))

    count = store._database.run_in_transaction(
        lambda t: release_credit(t, pt, ws, 500_000, 0)  # more than the 200k held
    )
    assert count == 0  # trips the caller's must-be-1 assert/alarm
    assert _typed(db, ws)["reserved"] == 200_000  # unchanged, never negative


def test_dml_after_mutation_is_rejected() -> None:
    """The fake fails fast on DML+mutation mixing (forbidden, docs §5), so future
    authorize/settle code that accidentally mixes is caught in tests."""
    import pytest

    store, _db, _ = make_fake_store()
    ws = "ws_mix"
    _seed_credit(store, ws, 1_000_000)
    pt = store._param_types

    def mix(transaction) -> None:
        store._write_entity_tx(transaction, "credit", ws, CreditAccount(
            workspace_id=ws, total_credits_microdollars=1_000_000))  # mutation
        reserve_credit(transaction, pt, ws, 100_000)  # DML -> must raise

    with pytest.raises(RuntimeError, match="DML.*mutation"):
        store._database.run_in_transaction(mix)


# ── key-limit conditional DML (the second hot row) ──────────────────────────

from trusted_router.storage_gcp_counter_dml import (  # noqa: E402
    KEY_ACCEPTED,
    KEY_INSUFFICIENT,
    KEY_MISSING,
    KEY_NO_HOLD,
    release_key,
    reserve_key,
)
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE  # noqa: E402


def _make_key(store, ws: str, *, limit, include_byok=True):
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None,
        limit_microdollars=limit, include_byok_in_limit=include_byok,
    )
    return key


def test_reserve_key_capped_no_overcap_under_concurrency() -> None:
    ws = "ws_keyhot"
    n = 8
    amount = 250_000  # 4 of 8 fit in a 1_000_000 cap
    barrier = threading.Barrier(n + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    key = _make_key(store, ws, limit=1_000_000)
    pt = store._param_types

    results: list[str] = []
    lock = threading.Lock()

    def reserve_once() -> None:
        r = store._database.run_in_transaction(
            lambda t: reserve_key(t, pt, key.hash, amount, is_byok=False)
        )
        with lock:
            results.append(r)

    _run_workers(
        [threading.Thread(target=reserve_once, daemon=True) for _ in range(n)], barrier
    )
    assert results.count(KEY_ACCEPTED) == 4, results
    assert results.count(KEY_INSUFFICIENT) == 4, results
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 1_000_000


def test_reserve_key_uncapped_is_no_hold() -> None:
    store, _db, _ = make_fake_store()
    key = _make_key(store, "ws_unc", limit=None)
    pt = store._param_types
    r = store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 999_999, is_byok=False)
    )
    assert r == KEY_NO_HOLD


def test_reserve_key_byok_excluded_is_no_hold() -> None:
    store, db, _ = make_fake_store()
    key = _make_key(store, "ws_byok", limit=1_000_000, include_byok=False)
    pt = store._param_types
    r = store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 100_000, is_byok=True)
    )
    assert r == KEY_NO_HOLD
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 0  # no hold taken


def test_reserve_key_insufficient_and_missing() -> None:
    store, _db, _ = make_fake_store()
    key = _make_key(store, "ws_ins", limit=100_000)
    pt = store._param_types
    assert store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 250_000, is_byok=False)
    ) == KEY_INSUFFICIENT
    assert store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, "nonexistent_key", 1, is_byok=False)
    ) == KEY_MISSING


def test_release_key_settles_usage_and_byok() -> None:
    store, db, _ = make_fake_store()
    key = _make_key(store, "ws_krel", limit=2_000_000)
    pt = store._param_types
    assert store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 500_000, is_byok=False)
    ) == KEY_ACCEPTED
    # settle as Credits usage
    count = store._database.run_in_transaction(
        lambda t: release_key(t, pt, key.hash, 500_000, 480_000, book_to_byok=False)
    )
    assert count == 1
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["reserved"] == 0
    assert row["usage"] == 480_000
    assert row["byok_usage"] == 0
