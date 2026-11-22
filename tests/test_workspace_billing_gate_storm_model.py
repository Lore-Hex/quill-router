# ruff: noqa: F811 - shared fixtures
"""A model of one workspace's burst on one credit row, across several processes.

Opt-in (``TR_WORKSPACE_GATE_STORM_MODEL=1``): it runs for about forty seconds
of wall time and measures latencies, so it is a model to read, not a unit
test to gate on.

    TR_WORKSPACE_GATE_STORM_MODEL=1 uv run pytest -q -s -p no:xdist \
        tests/test_workspace_billing_gate_storm_model.py

What is real: the route's authorize, the store's typed finalize, the gate,
and the budgets (the reserve's 20 s, and a 2 s deadline around the finalize,
which is the snapshot settle's). What is modelled, around the Spanner fake's
credit-row transactions:

* one credit row and one key row with shared and exclusive locks and
  wound-wait: an older transaction aborts a younger holder it conflicts with
  and waits for an older one, and a freed lock goes to the oldest waiter;
* the reserve takes the credit row exclusively at once (its scan is hinted),
  and so does the settle release of each row, as the statements now do;
  ``MODEL_HINTED_SETTLE=0`` gives the settle its earlier shape, a shared
  lock on each row that it then upgrades;
* about 10 ms a statement and 21 ms a commit, which gives the reserve and the
  settle their measured uncontended times (about 41 ms and 55 ms).
  ``MODEL_SLOWDOWN`` multiplies both, and ``MODEL_ABORT_COST`` adds seconds
  to every aborted attempt before it retries;
* ``MODEL_PROCESSES`` processes, each with its own gate (and
  ``MODEL_MAX_WAITERS`` waiters per workspace in each), and
  ``MODEL_CLIENTS`` clients that send the next request as soon as the last
  one finishes: authorize, a provider call of ``MODEL_UPSTREAM`` seconds,
  then the settle, retried twice more when it misses its 2 s (each miss is a
  503 the gateway would have been answered). ``MODEL_WAIT_MS`` sets the
  gate's per-request wait bound.

It is not Spanner. The lock queue's order, the retry delays and the latencies
are assumptions, and a real burst's numbers will differ. With its default
timings it was too kind to the gate alone: it showed no settle past its two
seconds, and the first real burst behind the gate still had them, with most
waiters timing out behind transactions that took a second or more. The two
calibration knobs exist to make the earlier shape as bad as it really was
before reading what the exclusive scan changes. What the model is for is
comparing mechanisms on the same assumptions: the gate off and on, and the
settle release with and without the exclusive scan.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import DeadlineExceeded, GoogleAPICallError

from tests.fakes.spanner import FakeAborted, FakeSpannerDatabase
from tests.test_settle_one_commit import (  # noqa: F401  (fixtures)
    _authorize_route,
    fixed_catalog,
    prod_store,
)
from trusted_router import storage_gcp_io, workspace_billing_gate
from trusted_router.config import Settings
from trusted_router.workspace_billing_gate import FifoKeyedGate

pytestmark = pytest.mark.skipif(
    os.environ.get("TR_WORKSPACE_GATE_STORM_MODEL") != "1",
    reason="a timing model, run on request: set TR_WORKSPACE_GATE_STORM_MODEL=1",
)

PROCESSES = int(os.environ.get("MODEL_PROCESSES", "8"))
CLIENTS = int(os.environ.get("MODEL_CLIENTS", "50"))
SECONDS = float(os.environ.get("MODEL_SECONDS", "15"))
UPSTREAM = float(os.environ.get("MODEL_UPSTREAM", "0.3"))
HINTED_SETTLE = os.environ.get("MODEL_HINTED_SETTLE", "1") == "1"
MAX_WAITERS = int(os.environ.get("MODEL_MAX_WAITERS", "8"))
WAIT_MS = int(os.environ.get("MODEL_WAIT_MS", "0"))  # 0: the setting's default
SLOWDOWN = float(os.environ.get("MODEL_SLOWDOWN", "1"))
ABORT_COST = float(os.environ.get("MODEL_ABORT_COST", "0"))
CALIBRATED = SLOWDOWN != 1 or ABORT_COST != 0
SNAPSHOT_SETTLE_BUDGET = 2.0
STATEMENT = 0.010 * SLOWDOWN
UPGRADE_GAP = 0.002 * SLOWDOWN
COMMIT = 0.021 * SLOWDOWN
COST = 49
AUTHORIZE = "tr_authorize"
SETTLES = frozenset({
    "tr_finalize", "tr_settle_one_commit", "tr_refund_one_commit", "tr_refund_finalize",
})
_JITTER = random.Random(20261121)  # noqa: S311 - model jitter, not a secret


def _jitter(low: float, high: float) -> float:
    return _JITTER.uniform(low, high)


class _Wounded(Exception):
    """This model transaction was aborted by an older one."""


class _ModelTransaction:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        # Kept across its retries, as Spanner keeps a retried transaction's
        # priority: an old transaction eventually wins.
        self.priority = time.monotonic()
        self.wounded = False


class _Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.aborts: Counter[str] = Counter()
        self.commits: Counter[str] = Counter()
        self.inside = 0
        self.peak = 0
        self.lock_wait_seconds = 0.0


class _Row:
    """One row's shared and exclusive locks under wound-wait."""

    def __init__(self, stats: _Stats) -> None:
        self._cond = threading.Condition()
        self._holders: dict[_ModelTransaction, str] = {}
        self._waiting: dict[_ModelTransaction, str] = {}
        self._stats = stats

    def _conflicts(self, txn: _ModelTransaction, mode: str) -> list[_ModelTransaction]:
        return [
            holder for holder, held in self._holders.items()
            if holder is not txn and (mode == "X" or held == "X")
        ]

    def acquire(self, txn: _ModelTransaction, mode: str) -> None:
        with self._cond:
            began = time.monotonic()
            upgrade = txn in self._holders
            self._waiting[txn] = mode
            try:
                while True:
                    if txn.wounded:
                        raise _Wounded
                    for holder in self._conflicts(txn, mode):
                        if holder.priority > txn.priority:
                            holder.wounded = True
                            del self._holders[holder]
                            self._stats.aborts[holder.tag] += 1
                    overtaking = not upgrade and any(
                        other is not txn and other.priority < txn.priority
                        and (mode == "X" or wanted == "X")
                        for other, wanted in self._waiting.items()
                    )
                    if not self._conflicts(txn, mode) and not overtaking:
                        self._holders[txn] = mode
                        return
                    remaining = storage_gcp_io.shared_rpc_budget_remaining()
                    if remaining is not None and remaining <= 0:
                        raise DeadlineExceeded("model: deadline exceeded waiting for a row lock")
                    self._cond.wait(0.005 if remaining is None else min(0.005, remaining))
            finally:
                self._waiting.pop(txn, None)
                self._stats.lock_wait_seconds += time.monotonic() - began
                self._cond.notify_all()

    def release(self, txn: _ModelTransaction) -> None:
        with self._cond:
            if self._holders.pop(txn, None) is not None:
                self._cond.notify_all()


def _install_row_model(patch: pytest.MonkeyPatch, stats: _Stats) -> None:
    credit, key = _Row(stats), _Row(stats)
    original = FakeSpannerDatabase.run_in_transaction

    def pause(seconds: float, txn: _ModelTransaction) -> None:
        time.sleep(seconds)
        if txn.wounded:
            raise _Wounded

    def take_rows(txn: _ModelTransaction) -> None:
        """Lock the hot rows the way this transaction's statements do."""
        delay = 0.005
        while True:
            credit.release(txn)
            key.release(txn)
            txn.wounded = False
            try:
                if txn.tag == AUTHORIZE:
                    pause(STATEMENT * _jitter(0.3, 1.0), txn)
                    credit.acquire(txn, "X")
                    pause(STATEMENT, txn)
                    return
                pause(STATEMENT, txn)  # the reservation read
                pause(STATEMENT * _jitter(0.3, 1.0), txn)  # the batch reaches the releases
                for row in (credit, key):
                    if not HINTED_SETTLE:
                        row.acquire(txn, "S")
                        pause(UPGRADE_GAP, txn)
                    row.acquire(txn, "X")
                pause(STATEMENT * 0.5, txn)
                return
            except _Wounded:
                credit.release(txn)
                key.release(txn)
                remaining = storage_gcp_io.shared_rpc_budget_remaining()
                if remaining is not None and remaining <= 0:
                    raise DeadlineExceeded("model: deadline exceeded after an abort") from None
                time.sleep(ABORT_COST + delay * _jitter(0.5, 1.5))
                delay = min(delay * 1.5, 0.2)

    def run(database: Any, fn: Any, **kwargs: Any) -> Any:
        tag = kwargs.get("transaction_tag")
        if tag != AUTHORIZE and tag not in SETTLES:
            return original(database, fn, **kwargs)
        txn = _ModelTransaction(str(tag))

        def modelled(fake_transaction: Any) -> Any:
            try:
                take_rows(txn)
                result = fn(fake_transaction)
                time.sleep(COMMIT)
            except BaseException:
                credit.release(txn)
                key.release(txn)
                raise
            if txn.wounded:
                credit.release(txn)
                key.release(txn)
                raise FakeAborted("model: aborted at commit")
            return result

        with stats.lock:
            stats.inside += 1
            stats.peak = max(stats.peak, stats.inside)
        try:
            result = original(database, modelled, **kwargs)
            with stats.lock:
                stats.commits[str(tag)] += 1
            return result
        finally:
            credit.release(txn)
            key.release(txn)
            with stats.lock:
                stats.inside -= 1

    patch.setattr(FakeSpannerDatabase, "run_in_transaction", run)


class _ProcessGates:
    """One gate per modelled process; a client thread belongs to one of them."""

    def __init__(self) -> None:
        self._gates = [FifoKeyedGate() for _ in range(PROCESSES)]
        self.local = threading.local()

    def acquire(self, subject: str, **bounds: Any) -> Any:
        return self._gates[self.local.process].acquire(subject, **bounds)

    def release(self, subject: str, token: int, **bounds: Any) -> None:
        self._gates[self.local.process].release(subject, token, **bounds)


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share))] if ordered else float("nan")


@pytest.mark.parametrize("gate_on", [False, True], ids=["gate_off", "gate_on"])
def test_one_workspaces_burst_on_one_row(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    caplog: pytest.LogCaptureFixture,
    gate_on: bool,
) -> None:
    store, db, key = prod_store
    db.typed["tr_credit_balance"][("ws-one-commit", 0)]["total_credits"] = 10**12
    gates = _ProcessGates()
    stats = _Stats()
    logging.getLogger("trusted_router").setLevel(logging.WARNING)
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    store.trust_settings = Settings(
        environment="test", service_surface="internal",
        workspace_billing_gate_width=1 if gate_on else 0,
        workspace_billing_gate_max_waiters=MAX_WAITERS,
    )
    if WAIT_MS:
        store.trust_settings.workspace_billing_gate_wait_ms = WAIT_MS
    authorize_ms: list[float] = []
    settle_ms: list[float] = []
    outcomes: Counter[str] = Counter()
    lock = threading.Lock()
    start = threading.Barrier(CLIENTS)
    stop_at = time.monotonic() + SECONDS + 1

    def settle_once(authorization_id: str) -> bool:
        # Each settle attempt, like each authorize, is a request of its own.
        workspace_billing_gate.forget_request_waits()
        began = time.monotonic()
        try:
            with storage_gcp_io.spanner_rpc_deadline(began + SNAPSHOT_SETTLE_BUDGET):
                result = store.typed_finalize_gateway_authorization_result(
                    authorization_id, success=True, actual_microdollars=COST,
                    selected_usage_type="Credits",
                )
            done = bool(result.finalized) and time.monotonic() < began + SNAPSHOT_SETTLE_BUDGET
        except (GoogleAPICallError, RuntimeError):
            done = False
        with lock:
            settle_ms.append((time.monotonic() - began) * 1000)
        return done

    def client(index: int) -> None:
        gates.local.process = index % PROCESSES
        start.wait(timeout=60)
        request = 0
        while time.monotonic() < stop_at:
            request += 1
            workspace_billing_gate.forget_request_waits()
            began = time.monotonic()
            try:
                authorized = _authorize_route(key, f"storm-{gate_on}-{index}-{request}")
            except HTTPException as exc:
                with lock:
                    outcomes[f"authorize_{exc.status_code}"] += 1
                    authorize_ms.append((time.monotonic() - began) * 1000)
                continue
            with lock:
                authorize_ms.append((time.monotonic() - began) * 1000)
            time.sleep(UPSTREAM)
            settled = False
            for attempt in range(3):  # the gateway's three snapshot attempts
                if settle_once(authorized["authorization_id"]):
                    settled = True
                    break
                with lock:
                    outcomes["settle_503"] += 1
                time.sleep(0.25 * (attempt + 1))
            with lock:
                outcomes["settled" if settled else "settle_fell_back"] += 1

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(workspace_billing_gate, "WORKSPACE_GATE", gates)
        _install_row_model(patch, stats)
        began = time.monotonic()
        with ThreadPoolExecutor(max_workers=CLIENTS) as pool:
            for future in [pool.submit(client, index) for index in range(CLIENTS)]:
                future.result(timeout=600)
        elapsed = time.monotonic() - began

    gate_records = [
        record for record in caplog.records
        if getattr(record, "metric", None) == "workspace_billing_gate"
    ]
    gate_lines = Counter(f"{record.outcome}:{record.kind}" for record in gate_records)  # type: ignore[attr-defined]
    gate_waits = [float(record.value) for record in gate_records]  # type: ignore[attr-defined]
    settle_commits = sum(count for tag, count in stats.commits.items() if tag in SETTLES)
    settle_aborts = sum(count for tag, count in stats.aborts.items() if tag in SETTLES)
    print(
        f"\nMODEL gate={'on' if gate_on else 'off'} hinted_settle={int(HINTED_SETTLE)} "
        f"clients={CLIENTS} processes={PROCESSES} upstream={UPSTREAM}s seconds={elapsed:.1f}\n"
        f"  settled={outcomes['settled']} settle_503_answers={outcomes['settle_503']} "
        f"settles_out_of_attempts={outcomes['settle_fell_back']} "
        f"authorize_503={outcomes['authorize_503']} requests_per_second={outcomes['settled'] / elapsed:.1f}\n"
        f"  settle attempt ms p50={_percentile(settle_ms, 0.5):.0f} "
        f"p99={_percentile(settle_ms, 0.99):.0f} max={max(settle_ms):.0f}\n"
        f"  authorize ms p50={_percentile(authorize_ms, 0.5):.0f} "
        f"p99={_percentile(authorize_ms, 0.99):.0f} max={max(authorize_ms):.0f} "
        f"of ten seconds or more={sum(ms >= 10_000 for ms in authorize_ms)}\n"
        f"  commits authorize={stats.commits[AUTHORIZE]} settle={settle_commits}; "
        f"aborts authorize={stats.aborts[AUTHORIZE]} settle={settle_aborts} "
        f"({settle_aborts / max(1, settle_commits):.2f} per settle commit)\n"
        f"  peak transactions on the rows={stats.peak} "
        f"row lock wait={stats.lock_wait_seconds / elapsed * 60:.0f} s/min\n"
        f"  gate lines={dict(gate_lines)}\n"
        f"  gate wait ms p50={_percentile(gate_waits, 0.5):.0f} p90={_percentile(gate_waits, 0.9):.0f} "
        f"wait bound ms={WAIT_MS or 'default'} slowdown={SLOWDOWN:g} abort cost s={ABORT_COST:g}"
    )
    assert outcomes["settled"] > 0
    if CALIBRATED:
        return  # read the numbers; the acceptance below is for the default timings
    assert outcomes["authorize_503"] == 0
    if gate_on:
        # The acceptance: no snapshot settle misses its two seconds, and no
        # reserve sits anywhere near its twenty.
        assert outcomes["settle_503"] == 0
        assert _percentile(settle_ms, 0.99) < SNAPSHOT_SETTLE_BUDGET * 1000
        assert _percentile(authorize_ms, 0.99) < 10_000
        assert stats.peak <= 2 * PROCESSES
