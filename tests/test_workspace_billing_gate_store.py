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
  starts and nothing about it;
* a caller over the waiter cap or past the wait bound runs as it does with
  the gate off, and is charged exactly once;
* every request-path transaction that writes the credit row takes the gate,
  by name, inside the deadline it already had; nothing takes it when the
  setting is off, the workspace is sharded, the caller has no deadline (the
  drain) or the authorize is a strict-budget key's.

The fake holds no locks, so it cannot show Spanner's lock queue, and the
Spanner emulator runs one read-write transaction at a time, so neither can
show the production effect in seconds of lock wait. See the pull request for
what was and was not measured.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import BackgroundTasks
from google.api_core.exceptions import Aborted

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
from trusted_router import storage_gcp, storage_gcp_io, workspace_billing_gate
from trusted_router.config import Settings
from trusted_router.services import settle_outbox_drain as drain_mod
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.workspace_billing_gate import (
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
    """Each test gets its own gate, so a failure cannot leave a slot behind."""
    gate = FifoKeyedGate()
    monkeypatch.setattr(workspace_billing_gate, "WORKSPACE_GATE", gate)
    return gate


@dataclass
class RowTraffic:
    """How many of the credit-row transactions are inside the fake at once.

    ``hold_seconds`` is spent between a transaction's statements and its
    commit, the window in which the real transaction holds its row.
    """

    hold_seconds: float = 0.002
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
            authorized = _authorize_route(key, f"gate-{label}-{index}-{round_index}")
            data, tasks = _settle_route(authorized)
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


def test_a_caller_over_the_waiter_cap_runs_at_once_and_is_charged_once(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, key = prod_store
    # One holder and one waiter per workspace: of eight clients, the rest go
    # around the gate, exactly as they run with it off.
    store.trust_settings = _gate_settings(workspace_billing_gate_max_waiters=1)
    caplog.set_level(logging.INFO, logger=workspace_billing_gate.__name__)
    traffic = RowTraffic()
    with pytest.MonkeyPatch.context() as patch:
        traffic.install(patch)
        settled, background, _seconds = _burst(key, workers=8, rounds=2, label="cap")
    for tasks in background:
        _run_background(tasks)
    _assert_charged_exactly_once(db, key, settled, 16)
    lines = _gate_lines(caplog)
    assert sum(count for (outcome, _kind), count in lines.items() if outcome == WAITERS_FULL) > 0
    assert TIMEOUT not in {outcome for outcome, _kind in lines}
    assert traffic.peak > 1
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
) -> Iterator[None]:
    """Replace the gate with one that only records who entered it."""

    @contextmanager
    def record(workspace_id: str, *, kind: str, budget_seconds: float, **_bounds: Any) -> Iterator[GatePass]:
        seen.append((workspace_id, kind, budget_seconds))
        yield GatePass(FREE, held=True)

    patch.setattr(storage_gcp, "workspace_billing_gate", record)
    yield


def test_every_credit_row_transaction_takes_the_gate_inside_its_own_budget(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
) -> None:
    store, db, key = prod_store
    store.trust_settings = _gate_settings()
    seen: list[tuple[str, str, float]] = []
    with pytest.MonkeyPatch.context() as patch, _recorded_gate(patch, seen):
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


def test_nothing_takes_the_gate_when_it_is_off_or_the_workspace_is_sharded(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
) -> None:
    store, _db, key = prod_store
    seen: list[tuple[str, str, float]] = []

    @storage_gcp_io.spanner_rpc_budget(20.0)
    def in_a_request(workspace_id: str | None, kind: str) -> str:
        with store._credit_row_gate(workspace_id, kind) as turn:
            assert turn.wait_seconds == 0.0
            return turn.outcome

    with pytest.MonkeyPatch.context() as patch, _recorded_gate(patch, seen):
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
        # A workspace this process knows to be sharded spreads its
        # transactions over rows by design: it is left alone.
        store._credit_shard_counts._put(WORKSPACE, 4)
        assert in_a_request(WORKSPACE, "authorize") == OFF
        # A caller with no Spanner deadline has nothing to wait inside.
        store._credit_shard_counts._put(WORKSPACE, 1)
        with store._credit_row_gate(WORKSPACE, "frozen_finalize") as turn:
            assert turn.outcome == OFF
        assert seen == []
        # One shard, or a count this process has not loaded (every workspace
        # starts with one): gated. The peek never loads the count.
        assert in_a_request(WORKSPACE, "authorize") == FREE
        store._credit_shard_counts.invalidate(WORKSPACE)
        assert in_a_request(WORKSPACE, "finalize") == FREE
        assert store._credit_shard_counts.peek(WORKSPACE) is None
        assert [kind for _ws, kind, _budget in seen] == ["authorize", "finalize"]


def test_the_wait_never_exceeds_a_quarter_of_the_budget_the_transaction_has_left(
    prod_store: tuple[Any, FakeSpannerDatabase, Any],
    fresh_gate: FifoKeyedGate,
) -> None:
    store, _db, _key = prod_store
    store.trust_settings = _gate_settings()
    holder, _passed = fresh_gate.acquire(WORKSPACE, width=1, max_waiters=8, wait_seconds=1)
    assert holder is not None

    @storage_gcp_io.spanner_rpc_budget(0.4)
    def transaction_with_400_ms_left() -> tuple[GatePass, float, float]:
        began = time.monotonic()
        with store._credit_row_gate(WORKSPACE, "frozen_finalize") as turn:
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
