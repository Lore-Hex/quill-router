"""Concurrency tests for SpannerBigtableStore.

Drives the real store code against an in-process Spanner fake that simulates
snapshot-isolation conflict-abort. The point is to verify that the surviving
ledger and Stripe idempotency paths exercise the transaction-retry contract
correctly under concurrent writers; the retired JSON credit reservation/finalize
transactions are no longer part of the GCP backend after C1.
"""

from __future__ import annotations

import json
import threading

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import ApiKey, CreditAccount
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


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
    assert db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]["total_credits"] == 5_000_000
    assert ("stripe_event", "evt_dup") in db.rows
    assert db.aborts >= n - 1, f"expected >= {n - 1} aborts, got {db.aborts}"


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
