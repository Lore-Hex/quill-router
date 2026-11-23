# ruff: noqa: F811 - shared fixtures
"""The workspace gate around the real authorize and settle transactions.

These drive the route handlers (authorize, settle, the two-commit fallback,
the drain's frozen apply) against the Spanner fake, whose transactions
conflict on a shared row the way the real ones do: a transaction that read a
counter row another transaction has since written is aborted and redone. A
short hold between a transaction's statements and its commit makes concurrent
transactions of one workspace overlap, so the test sees how many are inside
the store at once and how many are redone, with the gate off and on.

What they pin:

* with the gate on, one workspace's credit-row transactions from this process
  are inside the store one at a time and none is redone; with it off they
  pile up and abort each other;
* the money is identical either way: the gate changes when a transaction
  starts, never what it writes;
* a caller over the waiter cap or past the wait bound runs as it does with
  the gate off, and is charged exactly once;
* a waiting settle goes before a waiting reserve;
* every request-path transaction that writes the credit row takes the gate,
  by name, inside the deadline it already had; nothing takes it when the
  setting is off, the transaction writes no credit row (BYOK only), the
  workspace is sharded or its shard count is not known to this process, the
  caller has no deadline (the drain) or the authorize is a strict-budget
  key's;
* what the wait costs: a near-deadline caller behind a slow holder waits at
  most a quarter of its budget at each of the four call sites, all of one
  request's waits add up to at most the configured bound, and a transaction
  that needed more than what the wait left it fails on its deadline in the
  way that call site already fails on one.

The fake holds no locks, so it cannot show Spanner's lock queue, and the
Spanner emulator runs one read-write transaction at a time, so neither can
show the production effect in seconds of lock wait. See the pull request for
what was and was not measured.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import BackgroundTasks, HTTPException
from google.api_core.exceptions import Aborted, DeadlineExceeded

from tests.fakes.spanner import FakeSpannerDatabase
from tests.test_settle_one_commit import (  # noqa: F401  (fixtures)
    INTERNAL,
    _authorize_route,
    _contend_one_commit,
    _money,
    _run_background,
    _settle_route,
    fixed_catalog,
    prod_store,
)
from trusted_router import (
    storage_gcp,
    storage_gcp_authorize,
    storage_gcp_io,
    workspace_billing_gate,
)
from trusted_router.config import Settings
from trusted_router.services import settle_outbox_drain as drain_mod
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.workspace_billing_gate import (
    ALLOWANCE_SPENT,
    FREE,
    OFF,
    TIMEOUT,
    WAITED,
    WAITERS_FULL,
    FifoKeyedGate,
    GatePass,
)

WORKSPACE = "ws-one-commit"
COST = 49  # one settle of the fixed catalog route, in microdollars
# Every transaction that writes the workspace's credit row.
CREDIT_ROW_TAGS = frozenset({
    "tr_authorize", "tr_settle_one_commit", "tr_refund_one_commit", "tr_finalize",
    "tr_refund_finalize",
})


def _gate_settings(**overrides: Any) -> Settings:
    options: dict[str, Any] = {
        "environment": "test", "service_surface": "internal",
        "workspace_billing_gate_width": 1,
        # Long enough that nothing in these tests falls through by time.
        "workspace_billing_gate_wait_ms": 5000,
    }
    options.update(overrides)
    return Settings(**options)


@pytest.fixture(autouse=True)
def fresh_gate(monkeypatch: pytest.MonkeyPatch) -> FifoKeyedGate:
    """Each test gets its own gate, so a failure cannot leave a slot behind,
    and starts as a new request with its full allowance of waits."""
    gate = FifoKeyedGate()
    monkeypatch.setattr(workspace_billing_gate, "WORKSPACE_GATE", gate)
    workspace_billing_gate.forget_request_waits()
    return gate


@dataclass
class RowTraffic:
    """How many of the credit-row transactions are inside the fake at once.

    ``hold_seconds`` is spent between a transaction's statements and its
    commit, the window in which the real transaction holds its row. With
    ``park_first`` the first credit-row transaction to reach that window
    stays there, inside the store, until ``release`` is set: a holder whose
    turn ends when the test says so, not when a loaded machine gets to it.
    """

    hold_seconds: float = 0.002
    park_first: bool = False
    parked: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    inside: int = 0
    peak: int = 0
    attempts: Counter[str] = field(default_factory=Counter)
    finished: Counter[str] = field(default_factory=Counter)
    lock: Any = field(default_factory=threading.Lock)

    def install(self, patch: pytest.MonkeyPatch) -> None:
        original = FakeSpannerDatabase.run_in_transaction
        traffic = self

        def run(database: Any, fn: Any, **kwargs: Any) -> Any:
            tag = kwargs.get("transaction_tag")
            if tag not in CREDIT_ROW_TAGS:
                return original(database, fn, **kwargs)

            def held(txn: Any) -> Any:
                with traffic.lock:
                    traffic.attempts[tag] += 1
                result = fn(txn)
                time.sleep(traffic.hold_seconds)
                with traffic.lock:
                    park = traffic.park_first and not traffic.parked.is_set()
                    if park:
                        traffic.parked.set()
                if park:
                    assert traffic.release.wait(timeout=60), "the parked holder was never released"
                return result

            with traffic.lock:
                traffic.inside += 1
                traffic.peak = max(traffic.peak, traffic.inside)
            try:
                return original(database, held, **kwargs)
            finally:
                with traffic.lock:
                    traffic.inside -= 1
                    traffic.finished[tag] += 1

        patch.setattr(FakeSpannerDatabase, "run_in_transaction", run)

    @property
    def redone(self) -> int:
        """Attempts beyond one per transaction: each is an abort and a redo."""
        return sum(self.attempts.values()) - sum(self.finished.values())


def _one_request(key: Any, idempotency_key: str) -> tuple[dict[str, Any], BackgroundTasks]:
    """A reserve and then its settle. The gateway sends them as two requests,
    and each route runs in a context of its own, so the settle does not share
    the reserve's allowance of waits either; a thread that plays several
    requests starts each one afresh here, as the gate's module asks of a
    loop."""
    workspace_billing_gate.forget_request_waits()
    authorized = _authorize_route(key, idempotency_key)
    workspace_billing_gate.forget_request_waits()
    return _settle_route(authorized)


def _burst(
    key: Any, *, workers: int, rounds: int, label: str,
) -> tuple[list[dict[str, Any]], list[BackgroundTasks], float]:
    """``workers`` clients of one workspace, each authorizing then settling
    ``rounds`` requests back to back, all started together."""
    start = threading.Barrier(workers)
    settled: list[dict[str, Any]] = []
    background: list[BackgroundTasks] = []
    lock = threading.Lock()

    def client(index: int) -> None:
        start.wait(timeout=10)
        for round_index in range(rounds):
            # Each request has its own allowance of waits, as each request
            # does in production (its own context); one thread plays several.
            data, tasks = _one_request(key, f"gate-{label}-{index}-{round_index}")
            with lock:
                settled.append(data)
                background.append(tasks)

    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(client, index) for index in range(workers)]:
            future.result(timeout=120)
    return settled, background, time.monotonic() - began


def _gate_lines(caplog: pytest.LogCaptureFixture) -> Counter[tuple[str, str]]:
    return Counter(
        (record.outcome, record.kind)  # type: ignore[attr-defined]
        for record in caplog.records
        if getattr(record, "metric", None) == "workspace_billing_gate"
    )


def _assert_charged_exactly_once(
    db: FakeSpannerDatabase, key: Any, settled: list[dict[str, Any]], requests: int,
) -> None:
    assert len(settled) == requests
    assert all(data["settled"] is True and data["disposition"] == "finalized" for data in settled)
    assert all(data["cost_microdollars"] == COST for data in settled)
    # Usage is the sum of the charges, and every hold is released, on both
    # the workspace's credit row and the key's cap row.
    assert _money(db, WORKSPACE, key.hash) == (COST * requests, 0, COST * requests, 0)
    rows = [row for (_aid, kind), row in db.settle_outbox.items() if kind == "settle"]
    assert len(rows) == requests
    assert all((row["status"], row["attempts"]) == ("done", 1) for row in rows)
    assert all(reservation["settled"] for reservation in db.reservations.values())
    assert len(db.generation_records) == requests


@pytest.mark.parametrize("gate_on", [False, True], ids=["gate_off", "gate_on"])
def test_one_workspaces_burst_runs_one_credit_row_transaction_at_a_time_behind_the_gate(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
    gate_on: bool,
) -> None:
    store, db, key = prod_store
    workers, rounds = 8, 3
    store.trust_settings = _gate_settings(workspace_billing_gate_width=1 if gate_on else 0)
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    traffic = RowTraffic()
    with pytest.MonkeyPatch.context() as patch:
        traffic.install(patch)
        settled, background, _seconds = _burst(key, workers=workers, rounds=rounds, label="burst")
    for tasks in background:
        _run_background(tasks)

    # The money does not depend on the gate.
    _assert_charged_exactly_once(db, key, settled, workers * rounds)
    assert traffic.finished["tr_authorize"] == traffic.finished["tr_settle_one_commit"] == 24
    lines = _gate_lines(caplog)
    if gate_on:
        # One at a time, so no transaction ever read a row another one then
        # wrote: nothing is aborted and redone.
        assert traffic.peak == 1
        assert traffic.redone == 0
        # The others waited their turn here, and none went around.
        assert {outcome for outcome, _kind in lines} == {WAITED}
        assert {kind for _outcome, kind in lines} == {"authorize", "settle_one_commit"}
    else:
        # Eight clients' transactions pile up on the one row and abort each
        # other; the gate wrote nothing because it was never entered.
        assert traffic.peak > 1
        assert traffic.redone > 0
        assert not lines
    # No slot or waiter is left behind either way.
    assert (fresh_gate.subjects(), fresh_gate.total_waiting()) == (0, 0)


# How long a waiter in the over-cap test waits for the holder before it goes
# around. Test-only, set on this test's own gate instance: production's
# configured bound (workspace_billing_gate_wait_ms, at most five seconds)
# and the quarter of the caller's budget are untouched, and keep their own
# tests (the wait-bound and near-deadline tests below, and
# test_workspace_billing_gate.py). Here the waiter must outlast a holder
# that the test keeps inside the store while the over-cap callers run, for
# as long as a loaded machine takes to run them (the test's other bounds,
# 30 and 60 seconds for a thread to arrive or finish and 120 for the
# burst's clients, are far longer than that), so its TIMEOUT means a slot
# that was never handed on, not a slow runner.
PATIENCE_SECONDS = 60.0


def _patient(gate: FifoKeyedGate, patch: pytest.MonkeyPatch) -> None:
    acquire = gate.acquire

    def patient(subject: str, *, wait_seconds: float, **bounds: Any) -> tuple[int | None, GatePass]:
        return acquire(
            subject, wait_seconds=PATIENCE_SECONDS if wait_seconds > 0 else wait_seconds, **bounds,
        )

    patch.setattr(gate, "acquire", patient)


def _wait_for(condition: Any, what: str) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.001)


def _gate_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records
        if getattr(record, "metric", None) == "workspace_billing_gate"
    ]


def test_a_caller_over_the_waiter_cap_runs_at_once_and_is_charged_once(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One holder and one waiter per workspace, both in place before the
    burst: every transaction of eight clients finds the waiters full and
    goes around the gate at once, waiting nothing, exactly as it runs with
    the gate off, and is charged once; and the waiter is handed the slot
    when the holder leaves.

    The holder is a real reserve that the test keeps inside the store until
    the eight clients are done, and the waiter a real reserve with a
    test-only patience, so neither outcome depends on the runner's speed.
    When the holder and the waiter were two of the burst's own transactions,
    the holder's transaction ran beside the callers that went around, was
    aborted and redone by their commits on the same row and starved of CPU
    with them; on a loaded runner it held the slot for seconds, past the
    waiter's bound (a quarter of a settle's five seconds), and the waiter
    went around as a timeout. That is the gate working as designed, and is
    pinned by the wait-bound tests below.
    """
    store, db, key = prod_store
    store.trust_settings = _gate_settings(workspace_billing_gate_max_waiters=1)
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    traffic = RowTraffic(park_first=True)
    settled: list[dict[str, Any]] = []
    background: list[BackgroundTasks] = []

    def client(label: str) -> None:
        data, tasks = _one_request(key, f"gate-cap-{label}")
        settled.append(data)
        background.append(tasks)

    pool = ThreadPoolExecutor(max_workers=2)
    clients: list[Any] = []
    with pytest.MonkeyPatch.context() as patch:
        traffic.install(patch)
        _patient(fresh_gate, patch)
        try:
            # The holder: the first reserve takes the slot and stays in the store.
            clients.append(pool.submit(client, "holder"))
            assert traffic.parked.wait(timeout=30), "the holder never reached the store"
            assert (fresh_gate.holders(WORKSPACE), fresh_gate.waiting(WORKSPACE)) == (1, 0)
            # The waiter: the next reserve takes the one place in the queue.
            clients.append(pool.submit(client, "waiter"))
            _wait_for(lambda: fresh_gate.waiting(WORKSPACE) == 1, "the waiter never queued")
            # Over the cap: eight clients' reserves and settles, two rounds each.
            over, over_background, _seconds = _burst(key, workers=8, rounds=2, label="cap")
            # They all finished while the slot was still held and the waiter
            # still waiting, so none of them can have had the slot.
            assert (fresh_gate.holders(WORKSPACE), fresh_gate.waiting(WORKSPACE)) == (1, 1)
            over_cap_records = _gate_records(caplog)
            over_cap = _gate_lines(caplog)
        finally:
            traffic.release.set()
            pool.shutdown(wait=False)
            # A worker that never finishes fails here, not at pytest's timeout.
            for future in clients:
                future.result(timeout=60)
    # Every gated transaction of the burst went around at once: none waited,
    # so none could time out.
    assert {outcome for outcome, _kind in over_cap} == {WAITERS_FULL}
    assert {kind for _outcome, kind in over_cap} == {"authorize", "settle_one_commit"}
    assert over_cap == Counter({
        (WAITERS_FULL, "authorize"): 16, (WAITERS_FULL, "settle_one_commit"): 16,
    })
    assert all(record.value == 0 for record in over_cap_records)  # type: ignore[attr-defined]
    assert len(over) == 16
    settled.extend(over)
    background.extend(over_background)
    for tasks in background:
        _run_background(tasks)
    # The burst's sixteen requests, the holder's and the waiter's.
    _assert_charged_exactly_once(db, key, settled, 18)
    lines = _gate_lines(caplog)
    assert sum(count for (outcome, _kind), count in lines.items() if outcome == WAITERS_FULL) > 0
    assert TIMEOUT not in {outcome for outcome, _kind in lines}
    # The holder left and the waiter got the slot. The two settles that
    # followed found it free or waited their turn; nothing went around.
    after = lines - over_cap
    assert after[(WAITED, "authorize")] == 1
    assert set(after) <= {(WAITED, "authorize"), (WAITED, "settle_one_commit")}
    # The over-cap callers' transactions ran beside the holder's.
    assert traffic.peak > 1
    assert (fresh_gate.subjects(), fresh_gate.total_waiting()) == (0, 0)


def test_a_waiting_settle_goes_before_a_reserve_that_arrived_first(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
) -> None:
    store, db, key = prod_store
    store.trust_settings = _gate_settings()
    authorized = _authorize_route(key, "gate-order-settle")
    # Another of the workspace's transactions holds the slot.
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None
    order: list[str | None] = []
    original = FakeSpannerDatabase.run_in_transaction

    def run(database: Any, fn: Any, **kwargs: Any) -> Any:
        if kwargs.get("transaction_tag") in CREDIT_ROW_TAGS:
            order.append(kwargs.get("transaction_tag"))
        return original(database, fn, **kwargs)

    results: dict[str, Any] = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
        reserve = threading.Thread(
            target=lambda: results.__setitem__("reserve", _authorize_route(key, "gate-order-reserve")),
        )
        reserve.start()
        deadline = time.monotonic() + 10
        while fresh_gate.waiting(WORKSPACE) != 1:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        settle = threading.Thread(
            target=lambda: results.__setitem__("settle", _settle_route(authorized)[0]),
        )
        settle.start()
        while fresh_gate.waiting(WORKSPACE) != 2:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert order == []  # both are waiting; neither has started
        fresh_gate.release(WORKSPACE, holder, width=1)
        reserve.join(timeout=30)
        settle.join(timeout=30)
        assert not reserve.is_alive() and not settle.is_alive()
    # The reserve arrived first; the settle started first.
    assert order == ["tr_settle_one_commit", "tr_authorize"]
    assert results["settle"]["settled"] is True and results["reserve"]["authorization_id"]
    assert _money(db, WORKSPACE, key.hash)[0] == COST
    assert (fresh_gate.subjects(), fresh_gate.total_waiting()) == (0, 0)


def test_a_caller_past_the_wait_bound_runs_as_before_and_is_charged_once(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, key = prod_store
    store.trust_settings = _gate_settings(workspace_billing_gate_wait_ms=20)
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    first = _authorize_route(key, "gate-bound-first")
    second = _authorize_route(key, "gate-bound-second")
    caplog.clear()
    inside = threading.Event()
    release = threading.Event()
    original = FakeSpannerDatabase.run_in_transaction
    slow_once = [True]

    def run(database: Any, fn: Any, **kwargs: Any) -> Any:
        if kwargs.get("transaction_tag") == "tr_settle_one_commit" and slow_once[0]:
            slow_once[0] = False
            inside.set()
            # The first settle sits on the row far longer than the bound.
            assert release.wait(timeout=30)
        return original(database, fn, **kwargs)

    results: dict[str, dict[str, Any]] = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
        slow = threading.Thread(
            target=lambda: results.__setitem__("first", _settle_route(first)[0]),
        )
        slow.start()
        assert inside.wait(timeout=10)
        began = time.monotonic()
        results["second"], _tasks = _settle_route(second)
        waited = time.monotonic() - began
        release.set()
        slow.join(timeout=30)
        assert not slow.is_alive()
    # It waited about its 20 ms and then settled without the slot, while the
    # first settle still held it.
    assert waited < 5.0
    assert _gate_lines(caplog) == Counter({(TIMEOUT, "settle_one_commit"): 1})
    assert all(data["settled"] is True for data in results.values())
    assert _money(db, WORKSPACE, key.hash) == (2 * COST, 0, 2 * COST, 0)
    assert (fresh_gate.subjects(), fresh_gate.total_waiting()) == (0, 0)


@contextmanager
def _recorded_gate(
    patch: pytest.MonkeyPatch, seen: list[tuple[str, str, float]],
    first: dict[str, bool] | None = None,
) -> Iterator[None]:
    """Replace the gate with one that only records who entered it."""

    @contextmanager
    def record(workspace_id: str, *, kind: str, budget_seconds: float, **bounds: Any) -> Iterator[GatePass]:
        seen.append((workspace_id, kind, budget_seconds))
        if first is not None:
            first[kind] = bounds["first"]
        yield GatePass(FREE, held=True)

    patch.setattr(storage_gcp, "workspace_billing_gate", record)
    yield


def test_every_credit_row_transaction_takes_the_gate_inside_its_own_budget(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
) -> None:
    store, db, key = prod_store
    store.trust_settings = _gate_settings()
    seen: list[tuple[str, str, float]] = []
    first: dict[str, bool] = {}
    with pytest.MonkeyPatch.context() as patch, _recorded_gate(patch, seen, first):
        # The ordinary request: a reserve, then the one-commit settle.
        authorized = _authorize_route(key, "gate-kinds-one-commit")
        data, tasks = _settle_route(authorized)
        _run_background(tasks)
        assert data["disposition"] == "finalized"
        assert [(ws, kind) for ws, kind, _budget in seen] == [
            (WORKSPACE, "authorize"), (WORKSPACE, "settle_one_commit"),
        ]
        # The wait is spent inside the budget the transaction already had:
        # the route's 20 seconds for the reserve, the attempt's 5 for the
        # one-commit settle.
        (_ws, _kind, authorize_budget), (_ws2, _kind2, settle_budget) = seen
        assert 15.0 < authorize_budget <= 20.0
        assert 4.0 < settle_budget <= 5.0

        # A one-commit settle that loses to the store, with the deferral off:
        # the two-commit finalize takes the gate too.
        seen.clear()
        authorized = _authorize_route(key, "gate-kinds-two-commit")
        inline = INTERNAL.model_copy(update={"settle_contention_defers_to_drain": False})
        with pytest.MonkeyPatch.context() as contended:
            _contend_one_commit(contended, Aborted("wounded on the credit row"))
            data, tasks = _settle_route(authorized, settings=inline)
            _run_background(tasks)
        assert data["disposition"] == "finalized"
        assert [kind for _ws, kind, _budget in seen] == [
            "authorize", "settle_one_commit", "finalize",
        ]

        # The same loss with the deferral on leaves the intent to a frozen
        # apply. Run inside a deadline, as the snapshot settle runs it (two
        # seconds), the frozen apply takes the gate.
        seen.clear()
        authorized = _authorize_route(key, "gate-kinds-frozen")
        with pytest.MonkeyPatch.context() as contended:
            _contend_one_commit(contended, Aborted("wounded on the credit row"))
            data, tasks = _settle_route(authorized)
            _run_background(tasks)
        assert data["disposition"] == "intent_durable"
        row = drain_mod.spanner_settle_outbox().get(authorized["authorization_id"], "settle")
        assert row is not None
        with storage_gcp_io.spanner_rpc_deadline(time.monotonic() + 2):
            assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
        assert [kind for _ws, kind, _budget in seen] == [
            "authorize", "settle_one_commit", "frozen_finalize",
        ]
        assert 1.0 < seen[-1][2] <= 2.0
        assert {ws for ws, _kind, _budget in seen} == {WORKSPACE}

        # The drain has no deadline to wait inside: a wait there would be
        # extra time, and its applies already take turns. It is not gated.
        seen.clear()
        authorized = _authorize_route(key, "gate-kinds-drain")
        with pytest.MonkeyPatch.context() as contended:
            _contend_one_commit(contended, Aborted("wounded on the credit row"))
            data, tasks = _settle_route(authorized)
            _run_background(tasks)
        assert data["disposition"] == "intent_durable"
        drained = drain_mod.drain_settle_outbox(10)["outcomes"]
        # This request's intent, and the replay of the one applied above.
        assert drained[ApplyOutcome.SETTLED_NOW] == 1 and sum(drained.values()) == 2
        assert [kind for _ws, kind, _budget in seen] == ["authorize", "settle_one_commit"]
    # Everything that completes a request waits ahead of a reserve.
    assert first == {
        "authorize": False, "settle_one_commit": True, "finalize": True, "frozen_finalize": True,
    }
    assert _money(db, WORKSPACE, key.hash) == (4 * COST, 0, 4 * COST, 0)


def test_a_refund_takes_the_gate(prod_store: tuple[Any, FakeSpannerDatabase, Any]) -> None:
    store, db, key = prod_store
    store.trust_settings = _gate_settings()
    seen: list[tuple[str, str, float]] = []
    with pytest.MonkeyPatch.context() as patch, _recorded_gate(patch, seen):
        authorized = _authorize_route(key, "gate-kinds-refund")
        data, tasks = _settle_route(authorized, success=False)
        _run_background(tasks)
    # The refund's one commit (tr_refund_one_commit) releases the hold and
    # books nothing, through the same store method and the same gate.
    assert data["settled"] is True
    assert [kind for _ws, kind, _budget in seen] == ["authorize", "settle_one_commit"]
    assert _money(db, WORKSPACE, key.hash) == (0, 0, 0, 0)


def test_nothing_takes_the_gate_unless_it_is_known_to_write_the_one_credit_row(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
) -> None:
    store, _db, key = prod_store
    seen: list[tuple[str, str, float]] = []

    @storage_gcp_io.spanner_rpc_budget(20.0)
    def in_a_request(workspace_id: str | None, kind: str, *, writes: bool = True) -> str:
        with store._credit_row_gate(workspace_id, kind, writes_credit_row=writes) as turn:
            assert turn.wait_seconds == 0.0
            return turn.outcome

    with pytest.MonkeyPatch.context() as patch, _recorded_gate(patch, seen):
        store._credit_shard_counts._put(WORKSPACE, 1)
        # No settings at all (a store built without them) and the default
        # setting both leave the gate out.
        for settings in (None, Settings(environment="test", service_surface="internal")):
            store.trust_settings = settings
            assert in_a_request(WORKSPACE, "authorize") == OFF
        data, tasks = _settle_route(_authorize_route(key, "gate-off"))
        _run_background(tasks)
        assert data["disposition"] == "finalized" and seen == []

        store.trust_settings = _gate_settings()
        # No workspace to key on (a finalize without its authorization).
        assert in_a_request(None, "frozen_finalize") == OFF
        # A transaction that writes no credit row has no row to take turns on.
        assert in_a_request(WORKSPACE, "authorize", writes=False) == OFF
        # A workspace this process knows to be sharded spreads its
        # transactions over rows by design: it is left alone.
        store._credit_shard_counts._put(WORKSPACE, 4)
        assert in_a_request(WORKSPACE, "authorize") == OFF
        # A count this process has not loaded (or whose cache entry expired)
        # is not guessed: the workspace may be sharded. The peek never loads.
        store._credit_shard_counts.invalidate(WORKSPACE)
        assert in_a_request(WORKSPACE, "finalize") == OFF
        assert store._credit_shard_counts.peek(WORKSPACE) is None
        # A caller with no Spanner deadline has nothing to wait inside.
        store._credit_shard_counts._put(WORKSPACE, 1)
        with store._credit_row_gate(WORKSPACE, "frozen_finalize", writes_credit_row=True) as turn:
            assert turn.outcome == OFF
        assert seen == []
        # Known to have exactly one credit shard, writing it, inside a
        # deadline: gated.
        assert in_a_request(WORKSPACE, "authorize") == FREE
        assert in_a_request(WORKSPACE, "finalize") == FREE
        assert [kind for _ws, kind, _budget in seen] == ["authorize", "finalize"]


def test_a_settle_is_never_gated_on_a_guess_about_the_shard_count(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An instance that has not loaded the workspace's shard count (it only
    sees its settles, or the cache entry expired) must not make two settles
    of a sharded workspace, on different rows, take turns: the one behind
    would lose part of its budget for nothing. Unknown means not gated."""
    store, db, key = prod_store
    store.trust_settings = _gate_settings()
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    authorized = [_authorize_route(key, f"gate-unknown-{index}") for index in range(3)]
    # Another settle of the workspace holds the slot for the whole test, as a
    # slow settle on another shard would.
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None
    caplog.clear()

    def settle(index: int) -> float:
        began = time.monotonic()
        data, tasks = _settle_route(authorized[index])
        elapsed = time.monotonic() - began
        _run_background(tasks)
        assert data["disposition"] == "finalized"
        return elapsed

    # The cache entry has expired on this instance: not gated, no wait.
    store._credit_shard_counts.invalidate(WORKSPACE)
    assert settle(0) < 1.0
    # Known to be sharded: not gated either.
    store._credit_shard_counts._put(WORKSPACE, 4)
    assert settle(1) < 1.0
    assert not _gate_lines(caplog)
    # Only once the process knows there is one credit row does the settle
    # wait for the holder (here: its quarter of the attempt's five seconds).
    store._credit_shard_counts._put(WORKSPACE, 1)
    assert 1.0 <= settle(2) < 3.0
    assert _gate_lines(caplog) == Counter({(TIMEOUT, "settle_one_commit"): 1})
    assert _money(db, WORKSPACE, key.hash) == (3 * COST, 0, 3 * COST, 0)
    fresh_gate.release(WORKSPACE, holder, width=1)


def test_a_byok_only_request_writes_no_credit_row_and_is_never_gated(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import datetime as dt

    store, _db, key = prod_store
    store.trust_settings = _gate_settings()
    store._credit_shard_counts._put(WORKSPACE, 1)
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    # Another of the workspace's transactions holds the slot throughout.
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None

    @storage_gcp_io.spanner_rpc_budget(20.0)
    def request(*, credit: bool) -> tuple[float, Any]:
        began = time.monotonic()
        usage = "Credits" if credit else "BYOK"
        outcome, authorization = store.authorize_gateway_typed(
            workspace_id=WORKSPACE, key_hash=key.hash, estimate=10,
            has_credit_candidate=credit, reservation_usage_type=usage,
            model_id="anthropic/claude-opus-4.7", provider="anthropic",
            requested_model_id="anthropic/claude-opus-4.7",
            candidate_model_ids=["anthropic/claude-opus-4.7"], region="us",
            endpoint_id="anthropic/claude-opus-4.7@anthropic/byok",
            candidate_endpoint_ids=["anthropic/claude-opus-4.7@anthropic/byok"],
            idempotency_key=None, idempotency_fingerprint=None,
            expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
        )
        assert authorization is not None, outcome
        result = store.typed_finalize_gateway_authorization_result(
            authorization.id, success=True, actual_microdollars=0 if not credit else 5,
            selected_usage_type=usage,
        )
        assert result.finalized is True
        return time.monotonic() - began, authorization

    # A BYOK-only reserve and its settle: no credit held, nothing waits,
    # nothing is written about the gate, although the slot is taken.
    elapsed, authorization = request(credit=False)
    assert str(authorization.usage_type) == "BYOK"
    assert elapsed < 1.0
    assert not _gate_lines(caplog)
    # The same request on credit does wait for the holder, at both its
    # transactions (a quarter of the budget each, here capped by the wait).
    store.trust_settings = _gate_settings(workspace_billing_gate_wait_ms=60)
    workspace_billing_gate.forget_request_waits()
    request(credit=True)
    lines = _gate_lines(caplog)
    assert lines[(TIMEOUT, "authorize")] == 1
    assert set(lines) <= {(TIMEOUT, "authorize"), (ALLOWANCE_SPENT, "finalize"), (TIMEOUT, "finalize")}
    fresh_gate.release(WORKSPACE, holder, width=1)


def test_the_wait_never_exceeds_a_quarter_of_the_budget_the_transaction_has_left(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
) -> None:
    store, _db, _key = prod_store
    store.trust_settings = _gate_settings()
    store._credit_shard_counts._put(WORKSPACE, 1)
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None

    @storage_gcp_io.spanner_rpc_budget(0.4)
    def transaction_with_400_ms_left() -> tuple[GatePass, float, float]:
        began = time.monotonic()
        with store._credit_row_gate(WORKSPACE, "frozen_finalize", writes_credit_row=True) as turn:
            waited = time.monotonic() - began
            remaining = storage_gcp_io.shared_rpc_budget_remaining()
            assert remaining is not None
            return turn, waited, remaining

    turn, waited, remaining = transaction_with_400_ms_left()
    # Five configured seconds, but only a quarter of the 400 ms it had: it
    # went around after about 100 ms with most of its budget left.
    assert (turn.outcome, turn.held) == (TIMEOUT, False)
    assert 0.08 <= waited < 0.3
    assert 0.1 < remaining <= 0.32
    fresh_gate.release(WORKSPACE, holder, width=1)
    assert fresh_gate.subjects() == 0


class _SlowStore:
    """Makes each credit-row transaction take ``work`` seconds and fail on
    its deadline as a Spanner RPC does, and records the budget it started
    with. The fake itself is instantaneous and has no deadlines."""

    def __init__(self, patch: pytest.MonkeyPatch, *, work: float = 0.0) -> None:
        self.started_with: list[float] = []
        original = FakeSpannerDatabase.run_in_transaction
        slow = self

        def run(database: Any, fn: Any, **kwargs: Any) -> Any:
            if kwargs.get("transaction_tag") in CREDIT_ROW_TAGS:
                remaining = storage_gcp_io.shared_rpc_budget_remaining()
                assert remaining is not None
                slow.started_with.append(remaining)
                time.sleep(work)
                left = storage_gcp_io.shared_rpc_budget_remaining()
                if left is not None and left <= 0:
                    raise DeadlineExceeded("deadline exceeded in the credit-row transaction")
            return original(database, fn, **kwargs)

        patch.setattr(FakeSpannerDatabase, "run_in_transaction", run)


def _near_deadline_call(
    kind: str, store: Any, key: Any, patch: pytest.MonkeyPatch, budget: float,
) -> Any:
    """Run one call site with ``budget`` seconds left for its credit-row
    transaction, and return what its caller gets."""
    if kind == "authorize":
        @storage_gcp_io.spanner_rpc_budget(budget)
        def reserve() -> Any:
            try:
                return _authorize_route(key, f"near-deadline-{time.monotonic_ns()}")
            except (DeadlineExceeded, HTTPException):
                return "storage_unavailable"

        return reserve()
    authorized = _authorize_route(key, f"near-deadline-{kind}-{time.monotonic_ns()}")
    workspace_billing_gate.forget_request_waits()  # the settle is its own request
    if kind == "settle_one_commit":
        # The attempt's own budget (five seconds in production), shortened.
        patch.setattr(storage_gcp_authorize, "ONE_COMMIT_SETTLE_BUDGET_SECONDS", budget)
        data, _tasks = _settle_route(authorized)
        return data["disposition"]
    if kind == "finalize":
        @storage_gcp_io.spanner_rpc_budget(budget)
        def finalize() -> Any:
            try:
                return store.typed_finalize_gateway_authorization_result(
                    authorized["authorization_id"], success=True, actual_microdollars=COST,
                    selected_usage_type="Credits",
                ).finalized
            except DeadlineExceeded:
                return "deadline_exceeded"

        return finalize()
    assert kind == "frozen_finalize"
    # A deferred settle leaves the frozen intent the snapshot settle applies.
    with pytest.MonkeyPatch.context() as contended:
        _contend_one_commit(contended, Aborted("wounded on the credit row"))
        data, _tasks = _settle_route(authorized)
    assert data["disposition"] == "intent_durable"
    row = drain_mod.spanner_settle_outbox().get(authorized["authorization_id"], "settle")
    assert row is not None
    workspace_billing_gate.forget_request_waits()
    with storage_gcp_io.spanner_rpc_deadline(time.monotonic() + budget):
        return apply_frozen_settle(row)


_SUCCESS = {
    "authorize": dict, "settle_one_commit": "finalized", "finalize": True,
    "frozen_finalize": ApplyOutcome.SETTLED_NOW,
}
# What each call site's caller gets when its transaction runs out of time,
# with or without the gate: the reserve's storage error (the app answers it
# 503 "storage unavailable", and the gateway retries that), the deferral to
# the drain, the error the inline finalize raises, and the park that the
# snapshot settle answers 503 for.
_ON_DEADLINE = {
    "authorize": "storage_unavailable", "settle_one_commit": "intent_durable",
    "finalize": "deadline_exceeded", "frozen_finalize": ApplyOutcome.PARK_TYPED_UNAVAILABLE,
}
_KINDS = ["authorize", "settle_one_commit", "finalize", "frozen_finalize"]


@pytest.mark.parametrize("kind", _KINDS)
def test_a_near_deadline_caller_behind_a_slow_holder_waits_a_quarter_and_then_runs(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    store, _db, key = prod_store
    store.trust_settings = _gate_settings()
    budget = 0.6
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    _authorize_route(key, "warm-the-shard-count")
    # A slow transaction of the workspace holds the slot throughout.
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None
    with pytest.MonkeyPatch.context() as patch:
        slow = _SlowStore(patch)
        caplog.clear()
        result = _near_deadline_call(kind, store, key, patch, budget)
    expected = _SUCCESS[kind]
    assert isinstance(result, dict) if expected is dict else result == expected
    # It waited once, for about a quarter of what it had, and went around.
    waits = [
        record.value for record in caplog.records  # type: ignore[attr-defined]
        if getattr(record, "metric", None) == "workspace_billing_gate"
        and record.kind == kind  # type: ignore[attr-defined]
    ]
    assert len(waits) == 1 and 0.6 * budget * 250 <= waits[0] <= budget * 250 + 80
    # And its transaction started with about three quarters of the budget.
    assert slow.started_with[-1] >= 0.55 * budget
    fresh_gate.release(WORKSPACE, holder, width=1)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("gate_on", [False, True], ids=["gate_off", "gate_on"])
def test_a_transaction_that_needs_more_than_the_wait_left_fails_as_on_any_deadline(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    kind: str,
    gate_on: bool,
) -> None:
    """The gate's cost, stated as a test. A transaction that needs 85% of its
    budget finishes with the gate off. Behind a slow holder with the gate on,
    it waits a quarter, has 75% left, and runs out of time: its caller gets
    what that call site already answers on a deadline, nothing new."""
    store, _db, key = prod_store
    store.trust_settings = _gate_settings(workspace_billing_gate_width=1 if gate_on else 0)
    budget = 0.6
    _authorize_route(key, "warm-the-shard-count")
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None
    with pytest.MonkeyPatch.context() as patch:
        setup = _SlowStore(patch)  # instantaneous while the request is set up
        del setup
        original = FakeSpannerDatabase.run_in_transaction
        armed = [False]

        def run(database: Any, fn: Any, **kwargs: Any) -> Any:
            if armed[0] and kwargs.get("transaction_tag") in CREDIT_ROW_TAGS:
                armed[0] = False  # the transaction under test, once
                time.sleep(0.85 * budget)
                left = storage_gcp_io.shared_rpc_budget_remaining()
                if left is not None and left <= 0:
                    raise DeadlineExceeded("deadline exceeded in the credit-row transaction")
            return original(database, fn, **kwargs)

        if kind == "authorize":
            armed[0] = True
            patch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
            result = _near_deadline_call(kind, store, key, patch, budget)
        else:
            # Arm after the request's own reserve: only its settle is slow.
            reserve = _authorize_route

            def reserve_then_arm(*args: Any, **kwargs: Any) -> Any:
                authorized = reserve(*args, **kwargs)
                armed[0] = kind != "frozen_finalize"
                return authorized

            patch.setattr(FakeSpannerDatabase, "run_in_transaction", run)
            patch.setattr(sys.modules[__name__], "_authorize_route", reserve_then_arm)
            if kind == "frozen_finalize":
                frozen = apply_frozen_settle

                def arm_then_apply(row: Any) -> Any:
                    armed[0] = True
                    return frozen(row)

                patch.setattr(sys.modules[__name__], "apply_frozen_settle", arm_then_apply)
            result = _near_deadline_call(kind, store, key, patch, budget)
    if gate_on:
        assert result == _ON_DEADLINE[kind]
    else:
        expected = _SUCCESS[kind]
        assert isinstance(result, dict) if expected is dict else result == expected
    fresh_gate.release(WORKSPACE, holder, width=1)


def test_one_requests_two_gated_transactions_share_one_allowance(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A settle whose one-commit attempt loses to the store and then
    finalizes inline meets the gate twice. Its waits are counted together:
    the second transaction does not wait again once the request's bound is
    spent."""
    store, db, key = prod_store
    store.trust_settings = _gate_settings(workspace_billing_gate_wait_ms=150)
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    authorized = _authorize_route(key, "gate-allowance")
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None
    workspace_billing_gate.forget_request_waits()
    caplog.clear()
    inline = INTERNAL.model_copy(update={"settle_contention_defers_to_drain": False})
    with pytest.MonkeyPatch.context() as contended:
        _contend_one_commit(contended, Aborted("wounded on the credit row"))
        data, tasks = _settle_route(authorized, settings=inline)
        _run_background(tasks)
    assert data["disposition"] == "finalized"
    # 150 ms at the one-commit attempt, none at the inline finalize.
    assert _gate_lines(caplog) == Counter({
        (TIMEOUT, "settle_one_commit"): 1, (ALLOWANCE_SPENT, "finalize"): 1,
    })
    waited_ms = sum(
        record.value for record in caplog.records  # type: ignore[attr-defined]
        if getattr(record, "metric", None) == "workspace_billing_gate"
    )
    assert 140 <= waited_ms <= 230
    assert _money(db, WORKSPACE, key.hash) == (COST, 0, COST, 0)
    fresh_gate.release(WORKSPACE, holder, width=1)


def test_a_strict_budget_authorize_keeps_its_own_slot_and_never_waits_at_the_gate(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict-budget key's authorize already takes turns on its per-key
    slot. Waiting here while holding that slot would make the key's next
    authorize a strict-budget-busy 503, so the two are never nested. Its
    settle holds no strict slot and takes the gate like any other."""
    from trusted_router import strict_budget

    store, db, _key = prod_store
    store.trust_settings = _gate_settings()
    order: list[str] = []
    original_slot = strict_budget.strict_budget_slot

    @contextmanager
    def slot(key_hash: str) -> Iterator[float]:
        order.append("strict_slot")
        with original_slot(key_hash) as deadline:
            yield deadline
        order.append("strict_slot_released")

    @contextmanager
    def gate(workspace_id: str, *, kind: str, **_bounds: Any) -> Iterator[GatePass]:
        order.append(f"gate:{kind}")
        yield GatePass(FREE, held=True)
        order.append("gate_released")

    monkeypatch.setattr(strict_budget, "strict_budget_slot", slot)
    monkeypatch.setattr(storage_gcp, "workspace_billing_gate", gate)
    _raw, strict_key = store.api_keys.create(
        workspace_id=WORKSPACE, name="strict", creator_user_id="user-one-commit",
        limit_microdollars=40_000_000, budget_strict=True,
    )
    authorized = _authorize_route(strict_key, "gate-strict-order")
    assert order == ["strict_slot", "strict_slot_released"]
    data, tasks = _settle_route(authorized)
    _run_background(tasks)
    assert data["disposition"] == "finalized"
    assert order == [
        "strict_slot", "strict_slot_released", "gate:settle_one_commit", "gate_released",
    ]
    assert _money(db, WORKSPACE, strict_key.hash) == (COST, 0, COST, 0)
