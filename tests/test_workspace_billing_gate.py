"""The process-local queue for one workspace's credit-row transactions.

The primitive: a slot is free at once when nobody holds it, callers wait in
arrival order, the wait is bounded and ends in running ungated, the waiters
are capped per workspace and per process, and a slot is given back on every
exit. Nothing here touches a store; tests/test_workspace_billing_gate_store.py
drives the real authorize and settle code through it.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from trusted_router import workspace_billing_gate as gate_module
from trusted_router.config import Settings
from trusted_router.workspace_billing_gate import (
    FREE,
    NO_BUDGET,
    OFF,
    REENTRANT,
    SUBJECTS_FULL,
    TIMEOUT,
    WAITED,
    WAITERS_FULL,
    FifoKeyedGate,
    ungated,
    wait_bound,
    workspace_billing_gate,
)

WS = "ws-gate"
LONG = 10.0


def _until(predicate: Any, *, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.001)


def _acquire(gate: FifoKeyedGate, subject: str = WS, **overrides: Any) -> tuple[int | None, Any]:
    options: dict[str, Any] = {"width": 1, "max_waiters": 8, "wait_seconds": LONG}
    options.update(overrides)
    return gate.acquire(subject, **options)


def test_a_free_slot_is_taken_at_once_and_the_entry_goes_when_it_is_given_back() -> None:
    gate = FifoKeyedGate()
    token, passed = _acquire(gate)
    assert token is not None
    assert (passed.outcome, passed.held, passed.wait_seconds, passed.queued) == (FREE, True, 0.0, 0)
    assert (gate.holders(WS), gate.waiting(WS), gate.subjects()) == (1, 0, 1)
    gate.release(WS, token, width=1)
    assert (gate.holders(WS), gate.waiting(WS), gate.subjects()) == (0, 0, 0)


def test_waiters_get_the_slot_in_arrival_order() -> None:
    gate = FifoKeyedGate()
    first, _ = _acquire(gate)
    assert first is not None
    order: list[int] = []
    outcomes: list[tuple[str, int]] = []
    threads = []

    def worker(index: int) -> None:
        token, passed = _acquire(gate)
        assert token is not None
        order.append(index)
        outcomes.append((passed.outcome, passed.queued))
        gate.release(WS, token, width=1)

    for index in range(5):
        thread = threading.Thread(target=worker, args=(index,))
        thread.start()
        threads.append(thread)
        # Arrival order is the order each caller joined the queue.
        _until(lambda expected=index + 1: gate.waiting(WS) == expected)
    assert gate.holders(WS) == 1 and gate.total_waiting() == 5
    gate.release(WS, first, width=1)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert order == [0, 1, 2, 3, 4]
    assert outcomes == [(WAITED, queued) for queued in range(5)]
    assert (gate.holders(WS), gate.total_waiting(), gate.subjects()) == (0, 0, 0)


def test_width_bounds_the_holders_and_the_rest_wait() -> None:
    gate = FifoKeyedGate()
    inside = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def worker() -> None:
        nonlocal inside, peak
        token, passed = _acquire(gate, width=2)
        assert token is not None and passed.held
        with lock:
            inside += 1
            peak = max(peak, inside)
        release.wait(10)
        with lock:
            inside -= 1
        gate.release(WS, token, width=2)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    _until(lambda: gate.holders(WS) == 2 and gate.waiting(WS) == 4)
    release.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert peak == 2
    assert gate.subjects() == 0


def test_a_wait_past_its_bound_ends_ungated_and_leaves_no_waiter() -> None:
    gate = FifoKeyedGate()
    holder, _ = _acquire(gate)
    assert holder is not None
    started = time.monotonic()
    token, passed = _acquire(gate, wait_seconds=0.05)
    elapsed = time.monotonic() - started
    assert token is None
    assert (passed.outcome, passed.held) == (TIMEOUT, False)
    assert 0.04 <= passed.wait_seconds <= elapsed < 2.0
    # The timed-out caller is gone from the queue: the holder's release finds
    # nobody to hand the slot to, and never grants it to a caller that left.
    assert (gate.holders(WS), gate.waiting(WS), gate.total_waiting()) == (1, 0, 0)
    gate.release(WS, holder, width=1)
    assert gate.subjects() == 0


def test_no_wait_budget_means_no_wait() -> None:
    gate = FifoKeyedGate()
    holder, _ = _acquire(gate)
    assert holder is not None
    started = time.monotonic()
    token, passed = _acquire(gate, wait_seconds=0.0)
    assert token is None and passed.outcome == NO_BUDGET and not passed.held
    assert time.monotonic() - started < 0.5
    assert gate.waiting(WS) == 0
    gate.release(WS, holder, width=1)


def test_one_workspace_parks_at_most_its_cap_and_the_next_caller_runs_at_once() -> None:
    gate = FifoKeyedGate()
    holder, _ = _acquire(gate)
    assert holder is not None
    done = threading.Event()
    waiters = [
        threading.Thread(target=lambda: (_acquire(gate, max_waiters=2), done.wait(10)))
        for _ in range(2)
    ]
    for thread in waiters:
        thread.start()
    _until(lambda: gate.waiting(WS) == 2)
    started = time.monotonic()
    token, passed = _acquire(gate, max_waiters=2)
    assert token is None
    assert (passed.outcome, passed.held, passed.queued) == (WAITERS_FULL, False, 2)
    assert time.monotonic() - started < 0.5
    assert gate.waiting(WS) == 2
    gate.release(WS, holder, width=1)
    done.set()
    for thread in waiters:
        thread.join(timeout=10)


def test_the_process_parks_at_most_its_total_and_other_workspaces_are_not_blocked() -> None:
    gate = FifoKeyedGate(max_total_waiters=2)
    holders = {}
    for subject in ("ws-a", "ws-b", "ws-c"):
        token, passed = _acquire(gate, subject)
        # A held workspace never delays another workspace's free slot.
        assert token is not None and passed.outcome == FREE
        holders[subject] = token
    done = threading.Event()
    parked = [
        threading.Thread(target=lambda s=subject: (_acquire(gate, s), done.wait(10)))
        for subject in ("ws-a", "ws-b")
    ]
    for thread in parked:
        thread.start()
    _until(lambda: gate.total_waiting() == 2)
    token, passed = _acquire(gate, "ws-c")
    assert token is None and passed.outcome == WAITERS_FULL
    assert gate.waiting("ws-c") == 0
    for subject, held in holders.items():
        gate.release(subject, held, width=1)
    done.set()
    for thread in parked:
        thread.join(timeout=10)


def test_the_map_is_bounded_and_a_caller_past_it_runs_ungated() -> None:
    gate = FifoKeyedGate(max_subjects=2)
    first, _ = _acquire(gate, "ws-a")
    second, _ = _acquire(gate, "ws-b")
    assert first is not None and second is not None
    token, passed = _acquire(gate, "ws-c")
    assert token is None and passed.outcome == SUBJECTS_FULL and not passed.held
    assert gate.subjects() == 2
    gate.release("ws-a", first, width=1)
    token, passed = _acquire(gate, "ws-c")
    assert token is not None and passed.outcome == FREE


def test_a_leaked_slot_stops_counting_and_its_late_release_is_ignored() -> None:
    now = [100.0]
    gate = FifoKeyedGate(abandoned_hold_seconds=60.0, clock=lambda: now[0])
    leaked, _ = _acquire(gate)
    assert leaked is not None
    now[0] += 59.0
    token, passed = _acquire(gate, wait_seconds=0.0)
    assert token is None and passed.outcome == NO_BUDGET
    now[0] += 1.0
    token, passed = _acquire(gate)
    assert token is not None and passed.outcome == FREE
    assert gate.abandoned == 1 and gate.holders(WS) == 1
    # The leaked holder finally returns: it must not free the new holder's slot.
    gate.release(WS, leaked, width=1)
    assert gate.holders(WS) == 1
    gate.release(WS, token, width=1)
    assert gate.subjects() == 0


def test_a_full_map_forgets_subjects_whose_only_holders_leaked() -> None:
    now = [0.0]
    gate = FifoKeyedGate(max_subjects=1, abandoned_hold_seconds=60.0, clock=lambda: now[0])
    leaked, _ = _acquire(gate, "ws-a")
    assert leaked is not None
    token, passed = _acquire(gate, "ws-b")
    assert token is None and passed.outcome == SUBJECTS_FULL
    now[0] = 61.0
    token, passed = _acquire(gate, "ws-b")
    assert token is not None and passed.outcome == FREE
    assert gate.subjects() == 1 and gate.holders("ws-a") == 0


def test_an_interrupted_wait_leaves_no_waiter_and_passes_on_a_slot_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = FifoKeyedGate()
    acquired, _ = _acquire(gate)
    assert acquired is not None
    holder: int = acquired

    class Interrupted(gate_module._Waiter):
        granted_first = False

        def __init__(self) -> None:
            super().__init__()
            waiter = self

            class Event:
                def wait(self, _timeout: float) -> bool:
                    if waiter.granted_first:
                        # The holder hands this waiter the slot, and only
                        # then does the wait blow up.
                        gate.release(WS, holder, width=1)
                        assert waiter.token is not None
                    raise KeyboardInterrupt

                def set(self) -> None:
                    return None

            self.event = Event()  # type: ignore[assignment]

    monkeypatch.setattr(gate_module, "_Waiter", Interrupted)
    with pytest.raises(KeyboardInterrupt):
        _acquire(gate)
    # Interrupted before any grant: the waiter is withdrawn, the holder stays.
    assert (gate.holders(WS), gate.waiting(WS), gate.total_waiting()) == (1, 0, 0)

    Interrupted.granted_first = True
    with pytest.raises(KeyboardInterrupt):
        _acquire(gate)
    # Interrupted after the grant: the slot it was given is released, so the
    # next caller finds it free instead of waiting on a caller that left.
    assert (gate.holders(WS), gate.waiting(WS), gate.subjects()) == (0, 0, 0)
    monkeypatch.undo()
    token, passed = _acquire(gate)
    assert token is not None and passed.outcome == FREE


def test_bounds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        FifoKeyedGate(max_subjects=0)
    with pytest.raises(ValueError):
        FifoKeyedGate(max_total_waiters=0)
    with pytest.raises(ValueError):
        FifoKeyedGate(abandoned_hold_seconds=0)
    gate = FifoKeyedGate()
    with pytest.raises(ValueError):
        gate.acquire("", width=1, max_waiters=1, wait_seconds=1)
    with pytest.raises(ValueError):
        gate.acquire(WS, width=0, max_waiters=1, wait_seconds=1)
    with pytest.raises(ValueError):
        gate.acquire(WS, width=1, max_waiters=0, wait_seconds=1)


@pytest.mark.parametrize(
    ("wait", "budget", "expected"),
    [
        (1.0, 20.0, 1.0),  # authorize's budget: the configured bound
        (1.0, 5.0, 1.0),  # the one-commit settle's budget
        (1.0, 1.2, 0.3),  # never more than a quarter of what is left
        (1.0, 0.0, 0.0),  # a spent budget waits for nothing
        (5.0, 2.0, 0.5),  # the snapshot settle's two seconds keep one and a half
    ],
)
def test_the_wait_is_the_configured_bound_and_at_most_a_quarter_of_the_budget_left(
    wait: float, budget: float, expected: float,
) -> None:
    assert wait_bound(wait, budget) == pytest.approx(expected)


def _gated(gate: FifoKeyedGate, **overrides: Any) -> Any:
    options: dict[str, Any] = {
        # A budget large enough that the configured wait is the bound.
        "width": 1, "max_waiters": 8, "wait_seconds": LONG, "budget_seconds": 3600.0,
        "kind": "settle_one_commit", "gate": gate,
    }
    options.update(overrides)
    return workspace_billing_gate(WS, **options)


def test_the_slot_is_given_back_when_the_transaction_raises() -> None:
    gate = FifoKeyedGate()
    with pytest.raises(RuntimeError, match="commit failed"):
        with _gated(gate) as passed:
            assert passed.held and gate.holders(WS) == 1
            raise RuntimeError("commit failed")
    assert gate.subjects() == 0
    with _gated(gate) as passed:
        assert passed.outcome == FREE


def test_a_nested_transaction_of_the_same_workspace_does_not_wait_for_itself() -> None:
    gate = FifoKeyedGate()
    with _gated(gate) as outer:
        assert outer.outcome == FREE
        started = time.monotonic()
        with _gated(gate) as inner:
            assert (inner.outcome, inner.held) == (REENTRANT, False)
            assert gate.holders(WS) == 1
        assert time.monotonic() - started < 0.5
        # The inner exit must not give the outer slot back.
        assert gate.holders(WS) == 1
    assert gate.subjects() == 0


def test_a_caller_that_times_out_still_runs_and_frees_nothing() -> None:
    gate = FifoKeyedGate()
    with _gated(gate):
        ran = []

        def late() -> None:
            with _gated(gate, wait_seconds=0.05) as passed:
                ran.append((passed.outcome, passed.held))

        thread = threading.Thread(target=late)
        thread.start()
        thread.join(timeout=10)
        assert ran == [(TIMEOUT, False)]
        # Its exit released nothing: the first caller still holds the slot.
        assert gate.holders(WS) == 1
    assert gate.subjects() == 0


def test_the_budget_caps_the_wait() -> None:
    gate = FifoKeyedGate()
    with _gated(gate):
        result = []

        def short_budget() -> None:
            started = time.monotonic()
            with _gated(gate, wait_seconds=LONG, budget_seconds=0.2) as passed:
                result.append((passed.outcome, time.monotonic() - started))

        thread = threading.Thread(target=short_budget)
        thread.start()
        thread.join(timeout=10)
    (outcome, waited), = result
    # A quarter of the 200 ms it had left, not the ten configured seconds.
    assert outcome == TIMEOUT and 0.04 <= waited < 2.0


def test_only_callers_that_waited_or_went_around_are_logged_and_never_by_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = FifoKeyedGate()
    caplog.set_level(logging.INFO, logger=gate_module.__name__)
    outcomes: list[str] = []

    def late() -> None:
        with _gated(gate, wait_seconds=0.05, kind="authorize") as passed:
            outcomes.append(passed.outcome)

    with _gated(gate):
        assert caplog.records == []  # the uncontended path logs nothing
        thread = threading.Thread(target=late)
        thread.start()
        thread.join(timeout=10)
    assert outcomes == [TIMEOUT]
    (record,) = caplog.records
    message = record.getMessage()
    assert message.startswith(
        "metric=workspace_billing_gate outcome=timeout kind=authorize wait_ms="
    )
    assert message.endswith(" queued=0")
    assert WS not in message
    assert (record.metric, record.outcome, record.kind, record.queued) == (  # type: ignore[attr-defined]
        "workspace_billing_gate", TIMEOUT, "authorize", 0,
    )
    assert WS not in repr(record.__dict__)


def test_a_failure_to_write_the_line_still_gives_the_slot_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = FifoKeyedGate()
    first, _ = _acquire(gate)
    assert first is not None
    entered = threading.Event()
    failures: list[BaseException] = []

    def broken_log(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("log sink failed")

    def waiter() -> None:
        entered.set()
        try:
            with _gated(gate):
                raise AssertionError("the body must not run when the line fails")
        except RuntimeError as exc:
            failures.append(exc)

    monkeypatch.setattr(gate_module.logger, "info", broken_log)
    thread = threading.Thread(target=waiter)
    thread.start()
    assert entered.wait(10)
    _until(lambda: gate.waiting(WS) == 1)
    # The waiter is handed the slot, then fails writing its "waited" line.
    gate.release(WS, first, width=1)
    thread.join(timeout=10)
    assert [str(exc) for exc in failures] == ["log sink failed"]
    # It did not keep the slot it had been given.
    assert (gate.holders(WS), gate.waiting(WS), gate.subjects()) == (0, 0, 0)


def test_the_gate_that_does_not_apply_runs_the_body_at_once() -> None:
    with ungated() as passed:
        assert (passed.outcome, passed.held, passed.wait_seconds) == (OFF, False, 0.0)


def test_the_setting_is_off_by_default_bounded_and_on_as_a_literal_in_rollout() -> None:
    settings = Settings(environment="test")
    # Off in code, so no revision inherits it; production turns it on in
    # rollout.sh's literal, and 0 there is the way back.
    assert settings.workspace_billing_gate_width == 0
    rollout = (Path(__file__).resolve().parents[1] / "scripts/deploy/rollout.sh").read_text()
    assert rollout.count('"TR_WORKSPACE_BILLING_GATE_WIDTH=1"') == 1
    assert rollout.count("TR_WORKSPACE_BILLING_GATE_WIDTH=") == 1
    assert settings.workspace_billing_gate_wait_ms == 1000
    assert settings.workspace_billing_gate_max_waiters == 8
    # The per-workspace cap stays below the per-process cap, which stays
    # below an instance's 16 request slots.
    assert 8 < gate_module.MAX_TOTAL_WAITERS < 16
    for bad in (
        {"workspace_billing_gate_width": -1},
        {"workspace_billing_gate_width": 5},
        {"workspace_billing_gate_wait_ms": 0},
        {"workspace_billing_gate_wait_ms": 5001},
        {"workspace_billing_gate_max_waiters": 0},
        {"workspace_billing_gate_max_waiters": 13},
    ):
        with pytest.raises(ValueError):
            Settings(environment="test", **bad)  # type: ignore[arg-type]
