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

import logging
import threading

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage import CreditAccount
from trusted_router.storage_gcp_counter_dml import release_credit, reserve_credit
from trusted_router.storage_gcp_counter_reconcile import audit_typed_invariants
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import Generation


def _floors() -> dict:
    # Recompute at call time (see test_billing_key_window_limits) — no
    # module-import capture that flakes across a UTC window boundary.
    return window_floors(utcnow())


def _seed_credit(store, ws: str, total: int) -> None:
    store._write_entity("credit", ws, CreditAccount(workspace_id=ws))
    store._database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws,
        "shard": 0,
        "total_credits": total,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }


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
    store, _db, _ = make_fake_store()
    ws = "ws_mix"
    _seed_credit(store, ws, 1_000_000)
    pt = store._param_types

    def mix(transaction) -> None:
        store._write_entity_tx(
            transaction,
            "credit",
            ws,
            CreditAccount(workspace_id=ws),
        )  # mutation
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
        lambda t: release_key(t, pt, key.hash, 500_000, 480_000, window_floors=_floors(), book_to_byok=False)
    )
    assert count == 1
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["reserved"] == 0
    assert row["usage"] == 480_000
    assert row["byok_usage"] == 0


def test_reserve_key_include_byok_true_counts_byok_usage() -> None:
    """With include_byok=true, prior BYOK usage consumes the cap headroom."""
    store, db, _ = make_fake_store()
    key = _make_key(store, "ws_ib", limit=1_000_000, include_byok=True)
    # BYOK usage is typed-DML-owned; the legacy JSON add_usage no longer
    # propagates into the typed row after the ownership split, so seed the typed
    # byok_usage directly (this is what reserve_key reads for the cap math).
    db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["byok_usage"] = 400_000
    pt = store._param_types
    # available = 1_000_000 - 0 - 400_000 - 0 = 600_000
    assert store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 700_000, is_byok=False)
    ) == KEY_INSUFFICIENT
    assert store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 500_000, is_byok=False)
    ) == KEY_ACCEPTED


def test_release_key_book_to_byok_and_underflow() -> None:
    store, db, _ = make_fake_store()
    key = _make_key(store, "ws_kb", limit=2_000_000)
    pt = store._param_types
    assert store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 300_000, is_byok=False)
    ) == KEY_ACCEPTED
    # settle as BYOK usage -> books byok_usage, not usage
    assert store._database.run_in_transaction(
        lambda t: release_key(t, pt, key.hash, 300_000, 250_000, window_floors=_floors(), book_to_byok=True)
    ) == 1
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["reserved"] == 0
    assert row["byok_usage"] == 250_000
    assert row["usage"] == 0
    # underflow: releasing more than held is a 0-row no-op (never negative)
    assert store._database.run_in_transaction(
        lambda t: release_key(t, pt, key.hash, 500_000, 0, window_floors=_floors(), book_to_byok=False)
    ) == 0
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 0


def test_reserve_key_uncapped_concurrent_no_aborts() -> None:
    """Concurrent reservers on an UNCAPPED key all get no-hold and the 0-row
    classification path does not introduce lock-upgrade contention."""
    n = 6
    barrier = threading.Barrier(n + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    key = _make_key(store, "ws_uncc", limit=None)
    pt = store._param_types
    results: list[str] = []
    lock = threading.Lock()

    def once() -> None:
        r = store._database.run_in_transaction(
            lambda t: reserve_key(t, pt, key.hash, 100_000, is_byok=False)
        )
        with lock:
            results.append(r)

    _run_workers([threading.Thread(target=once, daemon=True) for _ in range(n)], barrier)
    assert results == [KEY_NO_HOLD] * n, results
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 0


# ── tr_reservation: durable hold + scoped idempotency + settle claim ─────────

from tests.fakes.spanner import FakeAlreadyExists  # noqa: E402
from trusted_router.storage_gcp_counter_dml import (  # noqa: E402
    claim_reservation,
    insert_reservation,
    read_reservation,
    read_reservation_by_idempotency,
)


def _insert_res(store, *, rid, scope=None, fingerprint=None, credit_hold=0, key_hold=0):
    def txn(t):
        insert_reservation(
            t, store._param_types,
            reservation_id=rid, workspace_id="ws", key_hash="kh",
            ws_shard=0, key_shard=0, credit_reserved_micro=credit_hold,
            key_reserved_micro=key_hold, hold_usage_type="Credits",
            idempotency_scope=scope, idempotency_fingerprint=fingerprint,
            expires_at="2026-01-01T00:00:00Z",
        )
    store._database.run_in_transaction(txn)


def test_reservation_insert_and_reads() -> None:
    store, _db, _ = make_fake_store()
    _insert_res(store, rid="r1", scope="ws#kh#abc", fingerprint="fp1", credit_hold=500_000)
    pt = store._param_types

    by_idem = store._database.run_in_transaction(
        lambda t: read_reservation_by_idempotency(t, pt, "ws#kh#abc")
    )
    assert by_idem["reservation_id"] == "r1"
    assert by_idem["credit_reserved_micro"] == 500_000
    assert by_idem["idempotency_fingerprint"] == "fp1"
    assert by_idem["settled"] is False

    by_id = store._database.run_in_transaction(lambda t: read_reservation(t, pt, "r1"))
    assert by_id["workspace_id"] == "ws"
    assert by_id["credit_reserved_micro"] == 500_000

    miss = store._database.run_in_transaction(
        lambda t: read_reservation_by_idempotency(t, pt, "nope")
    )
    assert miss is None


def test_reservation_duplicate_idempotency_scope_raises_already_exists() -> None:
    store, _db, _ = make_fake_store()
    _insert_res(store, rid="r1", scope="dup")
    with pytest.raises(FakeAlreadyExists):
        _insert_res(store, rid="r2", scope="dup")  # same scope -> unique conflict


def test_reservation_concurrent_same_scope_one_wins() -> None:
    """Two concurrent first-calls with the same idempotency scope: exactly one
    INSERT commits, the other raises ALREADY_EXISTS (codex Step-3 #4) — the loser
    is NOT silently retried into a second debit."""
    barrier = threading.Barrier(3)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    outcomes: list[str] = []
    lock = threading.Lock()

    def insert(rid: str) -> None:
        try:
            _insert_res(store, rid=rid, scope="race")
            with lock:
                outcomes.append("ok")
        except FakeAlreadyExists:
            with lock:
                outcomes.append("already_exists")

    _run_workers(
        [threading.Thread(target=insert, args=(f"r{i}",), daemon=True) for i in range(2)],
        barrier,
    )
    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("already_exists") == 1, outcomes
    assert len(db.reservation_idemp) == 1


def test_reservation_claim_first_writer_wins() -> None:
    store, _db, _ = make_fake_store()
    _insert_res(store, rid="rc", credit_hold=100_000)
    pt = store._param_types
    first = store._database.run_in_transaction(
        lambda t: claim_reservation(t, pt, "rc", actual_micro=95_000, settled_usage_type="Credits")
    )
    second = store._database.run_in_transaction(
        lambda t: claim_reservation(t, pt, "rc", actual_micro=99_000, settled_usage_type="Credits")
    )
    assert first is True
    assert second is False  # already settled -> replay no-op
    row = store._database.run_in_transaction(lambda t: read_reservation(t, pt, "rc"))
    assert row["settled"] is True
    assert row["settled_usage_type"] == "Credits"
    assert row["actual_micro"] == 95_000  # first claim's actual, durably recorded


def test_reservation_duplicate_id_raises_already_exists() -> None:
    store, _db, _ = make_fake_store()
    _insert_res(store, rid="dupid")
    with pytest.raises(FakeAlreadyExists):
        _insert_res(store, rid="dupid", scope="different")  # same PK -> conflict


def test_reservation_claim_race_settles_once() -> None:
    barrier = threading.Barrier(7)
    store, _db, _ = make_fake_store(ready_barrier=barrier)
    _insert_res(store, rid="rr", credit_hold=100_000)
    pt = store._param_types
    wins: list[bool] = []
    lock = threading.Lock()

    def claim() -> None:
        w = store._database.run_in_transaction(
            lambda t: claim_reservation(t, pt, "rr", actual_micro=90_000, settled_usage_type="Credits")
        )
        with lock:
            wins.append(w)

    _run_workers([threading.Thread(target=claim, daemon=True) for _ in range(6)], barrier)
    assert wins.count(True) == 1, wins  # exactly one claim wins


# ── 3b-3: the atomic authorize transaction (keystone) ───────────────────────

import json as _json  # noqa: E402

from trusted_router.storage_gcp_authorize import (  # noqa: E402
    AuthorizeOutcome,
    authorize_atomic,
)
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE as _KLT  # noqa: E402


def _auth_body(aid, rid):
    return _json.dumps({"id": aid, "credit_reservation_id": rid, "model": "m"})


def _authorize(store, *, ws, key_hash, estimate, has_credit=True, scope=None, fp=None,
               expires="2026-01-01T00:00:00Z"):
    return authorize_atomic(
        store._database, store._param_types,
        workspace_id=ws, key_hash=key_hash, estimate=estimate,
        has_credit_candidate=has_credit,
        reservation_usage_type=("Credits" if has_credit else "BYOK"),
        idempotency_scope=scope, idempotency_fingerprint=fp,
        expires_at=expires, build_auth_body=_auth_body,
    )


def test_authorize_atomic_accepts_and_holds_both() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_auth"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    res = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    assert _typed(db, ws)["reserved"] == 1_000_000  # credit hold
    assert db.typed[_KLT][(key.hash, 0)]["reserved"] == 1_000_000  # key hold
    # reservation + auth entity written atomically
    assert res["reservation_id"] in db.reservations
    assert ("gateway_authorization", res["authorization_id"]) in db.rows
    resv = db.reservations[res["reservation_id"]]
    assert resv["credit_reserved_micro"] == 1_000_000
    assert resv["key_reserved_micro"] == 1_000_000
    assert resv["authorization_id"] == res["authorization_id"]


def test_authorize_atomic_insufficient_credits_leaks_no_hold() -> None:
    """THE atomicity test (codex#1 #1): credit reject must roll back the key hold
    taken earlier in the same transaction — no leaked reserved."""
    store, db, _ = make_fake_store()
    ws = "ws_auth_poor"
    _seed_credit(store, ws, 500_000)  # less than the estimate
    key = _make_key(store, ws, limit=5_000_000)  # key has plenty
    res = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    assert res["outcome"] == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert _typed(db, ws)["reserved"] == 0  # credit untouched
    assert db.typed[_KLT][(key.hash, 0)]["reserved"] == 0  # KEY HOLD ROLLED BACK
    assert db.reservations == {}  # nothing persisted


def test_authorize_atomic_key_limit_exceeded() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_auth_cap"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=100_000)  # tiny cap
    res = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    assert res["outcome"] == AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert _typed(db, ws)["reserved"] == 0  # credit never touched (key checked first)
    assert db.reservations == {}


def test_authorize_atomic_idempotent_replay() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_auth_idem"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    first = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, scope="s1", fp="fp1")
    assert first["outcome"] == AuthorizeOutcome.ACCEPTED
    reserved_after_first = _typed(db, ws)["reserved"]

    replay = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, scope="s1", fp="fp1")
    assert replay["outcome"] == AuthorizeOutcome.REPLAY
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["reservation_id"] == first["reservation_id"]
    assert _typed(db, ws)["reserved"] == reserved_after_first  # NO second debit
    assert len(db.reservations) == 1


def test_authorize_atomic_idempotency_fingerprint_mismatch() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_auth_mm"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, scope="s2", fp="fpA")
    mism = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, scope="s2", fp="fpB")
    assert mism["outcome"] == AuthorizeOutcome.IDEMPOTENCY_MISMATCH


def test_authorize_atomic_byok_no_credit_hold() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_auth_byok"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000, include_byok=True)
    res = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, has_credit=False)
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    assert _typed(db, ws)["reserved"] == 0  # no credit hold for BYOK
    assert db.typed[_KLT][(key.hash, 0)]["reserved"] == 1_000_000  # key hold (include_byok)
    assert db.reservations[res["reservation_id"]]["credit_reserved_micro"] == 0


def test_authorize_atomic_concurrent_same_scope_one_debit() -> None:
    """Two concurrent first-calls, same idempotency scope: exactly one debits,
    the other replays (ALREADY_EXISTS -> replay) — never a double reservation."""
    ws = "ws_auth_race"
    barrier = threading.Barrier(3)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    outcomes: list[str] = []
    lock = threading.Lock()

    def go() -> None:
        r = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, scope="race", fp="fp")
        with lock:
            outcomes.append(r["outcome"])

    _run_workers([threading.Thread(target=go, daemon=True) for _ in range(2)], barrier)
    assert outcomes.count(AuthorizeOutcome.ACCEPTED) == 1, outcomes
    assert outcomes.count(AuthorizeOutcome.REPLAY) == 1, outcomes
    assert len(db.reservations) == 1  # exactly one reservation
    assert _typed(db, ws)["reserved"] == 1_000_000  # one debit


# ── 3c: claim-gated settle ──────────────────────────────────────────────────

from trusted_router.storage_gcp_authorize import SettleOutcome, settle_atomic  # noqa: E402


def _settle(store, *, rid, actual, settled_ut="Credits", success=True):
    return settle_atomic(
        store._database, store._param_types,
        reservation_id=rid, actual_micro=actual,
        settled_usage_type=settled_ut, success=success,
    )


def test_settle_end_to_end_books_actual_on_both() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_e2e"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    assert auth["outcome"] == AuthorizeOutcome.ACCEPTED

    res = _settle(store, rid=auth["reservation_id"], actual=900_000)
    assert res["outcome"] == SettleOutcome.SETTLED
    credit = _typed(db, ws)
    assert credit["reserved"] == 0
    assert credit["total_usage"] == 900_000  # available now 4.1M
    krow = db.typed[_KLT][(key.hash, 0)]
    assert krow["reserved"] == 0
    assert krow["usage"] == 900_000
    assert krow["byok_usage"] == 0


def test_settle_refund_releases_without_booking() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_refund"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    res = _settle(store, rid=auth["reservation_id"], actual=0, success=False)
    assert res["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 0  # refund books nothing
    assert db.typed[_KLT][(key.hash, 0)]["usage"] == 0


def test_settle_replay_does_not_double_apply() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_settle_replay"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    first = _settle(store, rid=auth["reservation_id"], actual=900_000)
    second = _settle(store, rid=auth["reservation_id"], actual=900_000)
    assert first["outcome"] == SettleOutcome.SETTLED
    assert second["outcome"] == SettleOutcome.ALREADY_SETTLED
    assert _typed(db, ws)["total_usage"] == 900_000  # booked exactly once


def test_settle_race_settles_once() -> None:
    ws = "ws_settle_race"
    barrier = threading.Barrier(7)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid = auth["reservation_id"]
    outcomes: list[str] = []
    lock = threading.Lock()

    def go() -> None:
        r = _settle(store, rid=rid, actual=900_000)
        with lock:
            outcomes.append(r["outcome"])

    _run_workers([threading.Thread(target=go, daemon=True) for _ in range(6)], barrier)
    assert outcomes.count(SettleOutcome.SETTLED) == 1, outcomes
    assert _typed(db, ws)["total_usage"] == 900_000  # charged exactly once
    assert _typed(db, ws)["reserved"] == 0


def test_settle_byok_books_byok_usage_only() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_settle_byok"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000, include_byok=True)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, has_credit=False)
    res = _settle(store, rid=auth["reservation_id"], actual=800_000, settled_ut="BYOK")
    assert res["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["total_usage"] == 0  # no credit usage for BYOK
    krow = db.typed[_KLT][(key.hash, 0)]
    assert krow["byok_usage"] == 800_000
    assert krow["usage"] == 0
    assert krow["reserved"] == 0


def test_settle_not_found() -> None:
    store, _db, _ = make_fake_store()
    res = _settle(store, rid="nonexistent", actual=100)
    assert res["outcome"] == SettleOutcome.NOT_FOUND


# ── 3d: crash reaper ────────────────────────────────────────────────────────

from trusted_router.storage_gcp_authorize import reap_expired_reservations  # noqa: E402

_NOW = "2026-06-01T00:00:00Z"  # after the default authorize expiry, before 2027


def test_reaper_reclaims_expired_unsettled_holds() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_reap"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)  # expires 2026-01-01
    assert _typed(db, ws)["reserved"] == 1_000_000

    reaped = reap_expired_reservations(store._database, store._param_types, now=_NOW)
    assert reaped == 1
    assert _typed(db, ws)["reserved"] == 0  # hold released
    assert _typed(db, ws)["total_usage"] == 0  # no charge (abandoned)
    assert db.typed[_KLT][(key.hash, 0)]["reserved"] == 0
    # reservation now settled -> a second reap is a no-op
    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0


def test_reaper_skips_not_yet_expired() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_reap_future"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, expires="2027-01-01T00:00:00Z")
    reaped = reap_expired_reservations(store._database, store._param_types, now=_NOW)
    assert reaped == 0
    assert _typed(db, ws)["reserved"] == 1_000_000  # hold preserved


def test_reaper_skips_already_settled() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_reap_settled"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    _settle(store, rid=auth["reservation_id"], actual=900_000)  # settled before reap
    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0


def test_reaper_vs_late_settle_one_wins() -> None:
    """A real settle landing as the reaper runs must not double-apply: the claim
    makes exactly one of {settle, reap} win."""
    ws = "ws_reap_race"
    barrier = threading.Barrier(3)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid = auth["reservation_id"]
    results: list[str] = []
    lock = threading.Lock()

    def settler() -> None:
        r = _settle(store, rid=rid, actual=900_000)
        with lock:
            results.append(("settle", r["outcome"]))

    def reaper() -> None:
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            pass
        reap_expired_reservations(store._database, store._param_types, now=_NOW)

    t_settle = threading.Thread(target=settler, daemon=True)
    t_reap = threading.Thread(target=reaper, daemon=True)
    t_settle.start()
    t_reap.start()
    try:
        barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        pass
    t_settle.join(timeout=10)
    t_reap.join(timeout=10)

    # The reservation is settled exactly once; usage is either the real charge
    # (settle won) or 0 (reaper won) — never both, never negative.
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] in (0, 900_000)
    assert db.reservations[rid]["settled"] is True


def test_authorize_atomic_concurrent_same_scope_different_fingerprint() -> None:
    """Concurrent same-scope but DIFFERENT-body calls: the winner is ACCEPTED, the
    loser hits the unique-index conflict and must get IDEMPOTENCY_MISMATCH (NOT a
    replay of the winner's authorization) — codex keystone review."""
    ws = "ws_auth_race_mm"
    barrier = threading.Barrier(3)
    store, _db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    outcomes: list[str] = []
    lock = threading.Lock()

    def go(fp: str) -> None:
        r = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000, scope="rscope", fp=fp)
        with lock:
            outcomes.append(r["outcome"])

    _run_workers(
        [threading.Thread(target=go, args=(f"fp{i}",), daemon=True) for i in range(2)],
        barrier,
    )
    assert outcomes.count(AuthorizeOutcome.ACCEPTED) == 1, outcomes
    assert outcomes.count(AuthorizeOutcome.IDEMPOTENCY_MISMATCH) == 1, outcomes


# ── 3e-1: full-DML typed finalize (reproduces legacy finalize) ──────────────

from trusted_router.storage_gcp_authorize import typed_finalize_atomic  # noqa: E402

_TS = "2026-02-01T00:00:00Z"


def _typed_finalize(store, *, rid, aid, actual, settled_ut="Credits", success=True, gen=True):
    writes = []
    if gen and success:
        writes = [
            ("generation", f"gen-{rid}", _json.dumps({"id": f"gen-{rid}", "cost": actual})),
            ("generation_by_workspace", f"genidx-{rid}", _json.dumps({"generation_id": f"gen-{rid}"})),
        ]
    auth_settled = _json.dumps({"id": aid, "settled": True})
    return typed_finalize_atomic(
        store._database, store._param_types,
        reservation_id=rid, authorization_id=aid, success=success,
        actual_micro=actual, settled_usage_type=settled_ut, now=_TS,
        auth_body_settled=auth_settled, generation_writes=writes,
    )


def _missing_key_release_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "trusted_router.storage_gcp_authorize"
        and "missing tr_key_limit row" in record.getMessage()
    ]


def test_reaper_reclaims_hold_after_api_key_deletion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_reap_deleted_key"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    assert auth["outcome"] == AuthorizeOutcome.ACCEPTED

    assert store.api_keys.delete(key.hash) is True
    assert (key.hash, 0) in db.typed.get(_KLT, {})
    before = audit_typed_invariants(store)
    assert before.clean

    with caplog.at_level(logging.WARNING, logger="trusted_router.storage_gcp_authorize"):
        reaped = reap_expired_reservations(store._database, store._param_types, now=_NOW)

    assert reaped == 1
    assert db.reservations[auth["reservation_id"]]["settled"] is True
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 0
    assert db.typed[_KLT][(key.hash, 0)]["reserved"] == 0
    assert _missing_key_release_warnings(caplog) == []
    assert audit_typed_invariants(store).clean


def test_typed_finalize_charges_credit_after_api_key_deletion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_finalize_deleted_key"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    assert store.api_keys.delete(key.hash) is True

    with caplog.at_level(logging.WARNING, logger="trusted_router.storage_gcp_authorize"):
        result = _typed_finalize(store, rid=rid, aid=aid, actual=900_000)

    assert result["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 900_000
    assert db.typed[_KLT][(key.hash, 0)]["reserved"] == 0
    assert db.typed[_KLT][(key.hash, 0)]["usage"] == 900_000
    assert ("generation", f"gen-{rid}") in db.rows
    assert _json.loads(db.rows[("gateway_authorization", aid)].body)["settled"] is True
    assert _missing_key_release_warnings(caplog) == []


def test_key_release_guard_failure_stays_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_key_guard_loud"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid = auth["reservation_id"]
    db.typed[_KLT][(key.hash, 0)]["reserved"] = 500_000

    with caplog.at_level(logging.WARNING, logger="trusted_router.storage_gcp_authorize"):
        result = _settle(store, rid=rid, actual=900_000)

    assert result["outcome"] == SettleOutcome.ERROR
    assert db.reservations[rid]["settled"] is False
    assert _typed(db, ws)["reserved"] == 1_000_000
    assert db.typed[_KLT][(key.hash, 0)]["reserved"] == 500_000
    assert _missing_key_release_warnings(caplog) == []


def test_normal_key_release_row_count_one_path_does_not_probe_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from trusted_router import storage_gcp_counter_dml

    def fail_if_called(*_args, **_kwargs) -> bool:
        raise AssertionError("row-count 1 release must not run the missing-row probe")

    monkeypatch.setattr(storage_gcp_counter_dml, "key_limit_exists", fail_if_called)
    store, db, _ = make_fake_store()
    ws = "ws_key_release_regression"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)

    with caplog.at_level(logging.WARNING, logger="trusted_router.storage_gcp_authorize"):
        result = _settle(store, rid=auth["reservation_id"], actual=900_000)

    assert result["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 900_000
    krow = db.typed[_KLT][(key.hash, 0)]
    assert krow["reserved"] == 0
    assert krow["usage"] == 900_000
    assert krow["byok_usage"] == 0
    assert _missing_key_release_warnings(caplog) == []


def test_typed_finalize_full_success() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_tf"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid, aid = auth["reservation_id"], auth["authorization_id"]

    res = _typed_finalize(store, rid=rid, aid=aid, actual=900_000)
    assert res["outcome"] == SettleOutcome.SETTLED
    # counters
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 900_000
    assert db.typed[_KLT][(key.hash, 0)]["usage"] == 900_000
    # generation entities written in the same txn
    assert ("generation", f"gen-{rid}") in db.rows
    assert ("generation_by_workspace", f"genidx-{rid}") in db.rows
    # auth marked settled
    assert _json.loads(db.rows[("gateway_authorization", aid)].body)["settled"] is True


def test_typed_finalize_refund_no_generation_no_booking() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_tf_refund"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid, aid = auth["reservation_id"], auth["authorization_id"]

    res = _typed_finalize(store, rid=rid, aid=aid, actual=0, success=False)
    assert res["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 0  # refund books nothing
    assert ("generation", f"gen-{rid}") not in db.rows  # no generation on failure
    assert _json.loads(db.rows[("gateway_authorization", aid)].body)["settled"] is True


def test_typed_finalize_replay_books_once() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_tf_replay"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    first = _typed_finalize(store, rid=rid, aid=aid, actual=900_000)
    second = _typed_finalize(store, rid=rid, aid=aid, actual=900_000)
    assert first["outcome"] == SettleOutcome.SETTLED
    assert second["outcome"] == SettleOutcome.ALREADY_SETTLED
    assert _typed(db, ws)["total_usage"] == 900_000  # booked once


def test_typed_finalize_race_books_once() -> None:
    ws = "ws_tf_race"
    barrier = threading.Barrier(7)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=1_000_000)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    outcomes: list[str] = []
    lock = threading.Lock()

    def go() -> None:
        r = _typed_finalize(store, rid=rid, aid=aid, actual=900_000)
        with lock:
            outcomes.append(r["outcome"])

    _run_workers([threading.Thread(target=go, daemon=True) for _ in range(6)], barrier)
    assert outcomes.count(SettleOutcome.SETTLED) == 1, outcomes
    assert _typed(db, ws)["total_usage"] == 900_000  # charged exactly once


# ── 3e-2: store wrappers + origin detection ─────────────────────────────────


def test_store_authorize_wrapper_and_origin_detection() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_wrap"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    res = store.authorize_gateway_atomic(
        workspace_id=ws, key_hash=key.hash, estimate=1_000_000,
        has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope=None, idempotency_fingerprint=None,
        expires_at="2026-01-01T00:00:00Z", build_auth_body=_auth_body,
    )
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    rid, aid = res["reservation_id"], res["authorization_id"]
    # origin detection: this reservation is typed and matches its authorization
    assert store.is_typed_reservation(rid, aid) is True
    # a JSON-origin (no tr_reservation) or mismatched auth is not typed
    assert store.is_typed_reservation(rid, "gwa-someone-else") is False
    assert store.is_typed_reservation("json-reservation-id", aid) is False
    assert store.is_typed_reservation(None, aid) is False


def test_store_typed_finalize_wrapper_and_reaper_wrapper() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_wrap2"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    res = store.authorize_gateway_atomic(
        workspace_id=ws, key_hash=key.hash, estimate=1_000_000,
        has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope=None, idempotency_fingerprint=None,
        expires_at="2026-01-01T00:00:00Z", build_auth_body=_auth_body,
    )
    rid, aid = res["reservation_id"], res["authorization_id"]
    out = store.typed_finalize_gateway(
        reservation_id=rid, authorization_id=aid, success=True, actual_micro=900_000,
        settled_usage_type="Credits", now=_TS,
        auth_body_settled=_json.dumps({"id": aid, "settled": True}),
        generation_writes=[("generation", f"g-{rid}", "{}")],
    )
    assert out["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["total_usage"] == 900_000
    # reaper wrapper: nothing expired+unsettled now
    assert store.reap_expired_reservations(now=_NOW) == 0


def test_typed_finalize_gateway_authorization_logs_split_timing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_wrap_timing"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    outcome, auth = store.authorize_gateway_typed(
        workspace_id=ws,
        key_hash=key.hash,
        estimate=1_000_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="m",
        provider="openai",
        requested_model_id=None,
        candidate_model_ids=["m"],
        region="us",
        endpoint_id="e",
        candidate_endpoint_ids=["e"],
        idempotency_key=None,
        idempotency_fingerprint=None,
        expires_at="2026-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert auth is not None
    generation = Generation(
        id="gen-wrap-timing",
        request_id="req-wrap-timing",
        workspace_id=ws,
        key_hash=key.hash,
        model="m",
        provider_name="OpenAI",
        app="typed-finalize-test",
        tokens_prompt=10,
        tokens_completion=5,
        total_cost_microdollars=900_000,
        usage_type="Credits",
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
    )

    with caplog.at_level(logging.INFO, logger="trusted_router.storage_gcp"):
        finalized = store.typed_finalize_gateway_authorization(
            auth.id,
            success=True,
            actual_microdollars=900_000,
            selected_usage_type="Credits",
            generation=generation,
        )

    assert finalized is True
    records = [
        record
        for record in caplog.records
        if record.name == "trusted_router.storage_gcp"
        and record.getMessage().startswith("typed finalize timing ")
    ]
    [record] = records
    message = record.getMessage()
    assert f"authorization_id={auth.id}" in message
    assert " spanner_ms=" in message
    assert " index_ms=" in message
    assert " attempts=1" in message
    assert isinstance(record.args, tuple)
    assert len(record.args) == 4
    assert record.args[0] == auth.id
    assert isinstance(record.args[1], float)
    assert isinstance(record.args[2], float)
    assert record.args[3] == 1


def test_typed_idempotency_lookup_survives_gate_changes() -> None:
    """get_typed_authorization_by_idempotency finds a typed auth INDEPENDENT of
    the cohort flag, so a retry replays (codex 3e route #2)."""
    store, _db, _ = make_fake_store()
    ws = "ws_idem_gate"
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    res = store.authorize_gateway_typed(
        workspace_id=ws, key_hash=key.hash, estimate=1_000_000,
        has_credit_candidate=True, reservation_usage_type="Credits",
        model_id="m", provider="openai", requested_model_id=None,
        candidate_model_ids=["m"], region="us", endpoint_id="e",
        candidate_endpoint_ids=["e"], idempotency_key="idem-1",
        idempotency_fingerprint="fp1", expires_at="2026-01-01T00:00:00Z",
    )
    assert res[0] == AuthorizeOutcome.ACCEPTED
    found = store.get_typed_authorization_by_idempotency(ws, key.hash, "idem-1")
    assert found is not None
    assert found.id == res[1].id  # same authorization, regardless of cohort flag
    assert store.get_typed_authorization_by_idempotency(ws, key.hash, "other") is None
