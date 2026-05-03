"""Concurrency tests for SpannerBigtableStore.

Drives the real store code against an in-process Spanner fake that simulates
snapshot-isolation conflict-abort. The point is to verify that the credit
ledger and Stripe idempotency paths actually exercise the transaction-retry
contract correctly under concurrent writers — which the in-memory store's
threading.RLock does not test."""

from __future__ import annotations

import json
import threading

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import (
    ApiKey,
    CreditAccount,
    GatewayAuthorization,
    Generation,
    Reservation,
)


def _seed_credit(store, workspace_id: str, total_microdollars: int) -> None:
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            total_credits_microdollars=total_microdollars,
        ),
    )


def _credit_account(db, workspace_id: str) -> dict:
    return json.loads(db.rows[("credit", workspace_id)].body)


def _run_workers(workers: list[threading.Thread], barrier: threading.Barrier) -> None:
    for thread in workers:
        thread.start()
    try:
        barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        pass
    for thread in workers:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in workers), "worker hang"


def test_concurrent_reservations_against_spanner_do_not_overspend() -> None:
    workspace_id = "ws_credit"
    n = 8
    amount = 250_000  # 4 of 8 should succeed against a 1_000_000 balance.
    barrier = threading.Barrier(n + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, workspace_id, 1_000_000)

    successes: list[bool] = []
    successes_lock = threading.Lock()

    def reserve_once() -> None:
        try:
            store.reserve(workspace_id, "key_1", amount)
        except ValueError:
            with successes_lock:
                successes.append(False)
            return
        with successes_lock:
            successes.append(True)

    _run_workers(
        [threading.Thread(target=reserve_once, daemon=True) for _ in range(n)],
        barrier,
    )

    assert successes.count(True) == 4, successes
    assert successes.count(False) == 4, successes

    account = _credit_account(db, workspace_id)
    assert account["reserved_microdollars"] == 1_000_000
    assert account["total_usage_microdollars"] == 0

    # The whole point of the test: the retry path was exercised.
    assert db.aborts >= n - 1, f"expected ≥{n - 1} aborts, got {db.aborts}"


def test_concurrent_stripe_credit_is_idempotent_under_retries() -> None:
    workspace_id = "ws_stripe"
    n = 8
    barrier = threading.Barrier(n + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, workspace_id, 0)

    results: list[bool] = []
    results_lock = threading.Lock()

    def credit_once() -> None:
        result = store.credit_workspace_once(workspace_id, 5_000_000, "evt_dup")
        with results_lock:
            results.append(result)

    _run_workers(
        [threading.Thread(target=credit_once, daemon=True) for _ in range(n)],
        barrier,
    )

    assert results.count(True) == 1, results
    assert results.count(False) == n - 1, results

    account = _credit_account(db, workspace_id)
    assert account["total_credits_microdollars"] == 5_000_000
    assert ("stripe_event", "evt_dup") in db.rows
    assert db.aborts >= n - 1, f"expected ≥{n - 1} aborts, got {db.aborts}"


def test_concurrent_settle_does_not_double_apply() -> None:
    workspace_id = "ws_settle"
    n = 8
    barrier = threading.Barrier(n + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, workspace_id, 1_000_000)

    reservation = Reservation(
        id="res_1",
        workspace_id=workspace_id,
        key_hash="key_1",
        amount_microdollars=400_000,
    )
    store._write_entity("reservation", reservation.id, reservation)
    # Account state matches an outstanding reservation so the math is meaningful.
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            total_credits_microdollars=1_000_000,
            reserved_microdollars=400_000,
        ),
    )

    def settle_once() -> None:
        store.settle("res_1", 350_000)

    _run_workers(
        [threading.Thread(target=settle_once, daemon=True) for _ in range(n)],
        barrier,
    )

    account = _credit_account(db, workspace_id)
    # Reservation released exactly once; usage charged exactly once.
    assert account["reserved_microdollars"] == 0
    assert account["total_usage_microdollars"] == 350_000
    reservation_row = json.loads(db.rows[("reservation", "res_1")].body)
    assert reservation_row["settled"] is True


def test_concurrent_key_limit_reservations_do_not_overspend() -> None:
    workspace_id = "ws_key_limit"
    n = 6
    amount = 200_000  # 5 of 6 should succeed against a 1_000_000 limit.
    barrier = threading.Barrier(n + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)

    api_key = ApiKey(
        hash="key_1",
        salt="salt",
        secret_hash="digest",  # noqa: S106 - placeholder test digest.
        lookup_hash="lookup",
        name="key",
        label="sk-tr...abcd",
        workspace_id=workspace_id,
        creator_user_id=None,
        limit_microdollars=1_000_000,
    )
    store._write_entity("api_key", api_key.hash, api_key)

    successes: list[bool] = []
    lock = threading.Lock()

    def reserve_once() -> None:
        try:
            store.reserve_key_limit(api_key.hash, amount, usage_type="Credits")
        except ValueError:
            with lock:
                successes.append(False)
            return
        with lock:
            successes.append(True)

    _run_workers(
        [threading.Thread(target=reserve_once, daemon=True) for _ in range(n)],
        barrier,
    )

    assert successes.count(True) == 5, successes
    assert successes.count(False) == 1, successes
    key_row = json.loads(db.rows[("api_key", api_key.hash)].body)
    assert key_row["reserved_microdollars"] == 1_000_000


def test_gateway_authorize_then_settle_charges_exactly_once() -> None:
    """End-to-end: a gateway authorize reserves credits, a parallel batch of
    settle calls drains the reservation exactly once, and a parallel batch of
    refund calls is a no-op once settled. Models the real attested-gateway
    flow under load (settle and refund races are both possible during
    crash-recovery)."""
    workspace_id = "ws_gw"
    barrier_size = 8
    barrier = threading.Barrier(barrier_size + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, workspace_id, 2_000_000)

    reservation = Reservation(
        id="res_gw",
        workspace_id=workspace_id,
        key_hash="key_1",
        amount_microdollars=500_000,
    )
    store._write_entity("reservation", reservation.id, reservation)
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            total_credits_microdollars=2_000_000,
            reserved_microdollars=500_000,
        ),
    )
    auth = GatewayAuthorization(
        id="gwa_1",
        workspace_id=workspace_id,
        key_hash="key_1",
        model_id="openai/gpt-4o-mini",
        provider="openai",
        usage_type="Credits",
        estimated_microdollars=500_000,
        credit_reservation_id=reservation.id,
    )
    store._write_entity("gateway_authorization", auth.id, auth)

    def race_once(index: int) -> None:
        # Half settle, half refund — they should converge to a single outcome.
        if index % 2 == 0:
            store.settle(reservation.id, 480_000)
        else:
            store.refund(reservation.id)

    _run_workers(
        [
            threading.Thread(target=race_once, args=(i,), daemon=True)
            for i in range(barrier_size)
        ],
        barrier,
    )

    account = _credit_account(db, workspace_id)
    reservation_row = json.loads(db.rows[("reservation", reservation.id)].body)
    assert reservation_row["settled"] is True
    assert account["reserved_microdollars"] == 0
    # First-writer-wins: usage is either the settle amount or zero (refund),
    # never both. The credit ledger must never go negative or be charged twice.
    assert account["total_usage_microdollars"] in {0, 480_000}
    available = (
        account["total_credits_microdollars"]
        - account["total_usage_microdollars"]
        - account["reserved_microdollars"]
    )
    assert 0 <= available <= 2_000_000


def test_concurrent_gateway_finalize_writes_generation_and_charges_once() -> None:
    workspace_id = "ws_gw_finalize"
    n = 8
    barrier = threading.Barrier(n + 1)
    store, db, bt = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, workspace_id, 2_000_000)

    api_key = ApiKey(
        hash="key_1",
        salt="salt",
        secret_hash="digest",  # noqa: S106 - placeholder test digest.
        lookup_hash="lookup",
        name="key",
        label="sk-tr...abcd",
        workspace_id=workspace_id,
        creator_user_id=None,
        limit_microdollars=2_000_000,
        reserved_microdollars=500_000,
    )
    reservation = Reservation(
        id="res_gw_finalize",
        workspace_id=workspace_id,
        key_hash=api_key.hash,
        amount_microdollars=500_000,
    )
    auth = GatewayAuthorization(
        id="gwa_finalize",
        workspace_id=workspace_id,
        key_hash=api_key.hash,
        model_id="openai/gpt-4o-mini",
        provider="openai",
        usage_type="Credits",
        estimated_microdollars=500_000,
        credit_reservation_id=reservation.id,
    )
    generation = Generation(
        id="gen_finalize",
        request_id="req_finalize",
        workspace_id=workspace_id,
        key_hash=api_key.hash,
        model="openai/gpt-4o-mini",
        provider="openai",
        provider_name="OpenAI",
        app="gateway",
        tokens_prompt=100,
        tokens_completion=50,
        total_cost_microdollars=480_000,
        usage_type="Credits",
        speed_tokens_per_second=50.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        created_at="2026-05-02T12:00:00Z",
    )
    store._write_entity("api_key", api_key.hash, api_key)
    store._write_entity("reservation", reservation.id, reservation)
    store._write_entity("gateway_authorization", auth.id, auth)
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            total_credits_microdollars=2_000_000,
            reserved_microdollars=500_000,
        ),
    )

    results: list[bool] = []
    lock = threading.Lock()

    def finalize_once() -> None:
        result = store.finalize_gateway_authorization(
            auth.id,
            success=True,
            actual_microdollars=generation.total_cost_microdollars,
            selected_usage_type="Credits",
            generation=generation,
        )
        with lock:
            results.append(result)

    _run_workers([threading.Thread(target=finalize_once, daemon=True) for _ in range(n)], barrier)

    assert results.count(True) == 1, results
    assert results.count(False) == n - 1, results
    account = _credit_account(db, workspace_id)
    key_row = json.loads(db.rows[("api_key", api_key.hash)].body)
    reservation_row = json.loads(db.rows[("reservation", reservation.id)].body)
    auth_row = json.loads(db.rows[("gateway_authorization", auth.id)].body)

    assert account["reserved_microdollars"] == 0
    assert account["total_usage_microdollars"] == generation.total_cost_microdollars
    assert key_row["reserved_microdollars"] == 0
    assert key_row["usage_microdollars"] == generation.total_cost_microdollars
    assert reservation_row["settled"] is True
    assert auth_row["settled"] is True
    assert ("generation", generation.id) in db.rows
    assert ("generation_by_workspace", f"{workspace_id}#2026-05-02#2026-05-02T12:00:00Z#{generation.id}") in db.rows
    assert sum(key.startswith(b"gen#gen_finalize") for key in bt.committed) == 1


def test_no_aborts_when_uncontended() -> None:
    """Sanity: a single thread reserving credits should not retry, ever.
    Catches a regression where the fake's conflict detection over-fires on
    pure read-then-write within one transaction."""
    workspace_id = "ws_solo"
    store, db, _ = make_fake_store()
    _seed_credit(store, workspace_id, 1_000_000)
    store.reserve(workspace_id, "key_1", 250_000)
    store.reserve(workspace_id, "key_1", 250_000)
    assert db.aborts == 0


@pytest.mark.parametrize("parallelism", [4, 16])
def test_parallelism_does_not_change_correctness(parallelism: int) -> None:
    workspace_id = f"ws_param_{parallelism}"
    barrier = threading.Barrier(parallelism + 1)
    store, db, _ = make_fake_store(ready_barrier=barrier)
    _seed_credit(store, workspace_id, parallelism * 100_000)

    successes: list[bool] = []
    lock = threading.Lock()

    def reserve_once() -> None:
        try:
            store.reserve(workspace_id, "key_1", 100_000)
        except ValueError:
            with lock:
                successes.append(False)
            return
        with lock:
            successes.append(True)

    _run_workers(
        [threading.Thread(target=reserve_once, daemon=True) for _ in range(parallelism)],
        barrier,
    )

    assert successes.count(True) == parallelism
    account = _credit_account(db, workspace_id)
    assert account["reserved_microdollars"] == parallelism * 100_000
