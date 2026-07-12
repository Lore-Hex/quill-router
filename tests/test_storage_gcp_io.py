from __future__ import annotations

from collections.abc import Callable

import pytest
from google.api_core.exceptions import Aborted

from trusted_router.storage_gcp_io import (
    TXN_BUDGET_SECONDS,
    run_in_transaction_with_retry,
)


class _RetryingDatabase:
    def __init__(self, aborts_before_success: int) -> None:
        self.aborts_before_success = aborts_before_success
        self.calls = 0
        self.timeouts: list[float | None] = []

    def run_in_transaction(
        self, func: Callable[..., str], *, timeout_secs: float | None = None
    ) -> str:
        self.calls += 1
        self.timeouts.append(timeout_secs)
        if self.calls <= self.aborts_before_success:
            raise Aborted("spanner aborted")
        return func("txn")


def _txn(_transaction: object) -> str:
    return "ok"


class _Clock:
    """Deterministic monotonic clock; ``sleep`` advances it so backoff spends budget."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.setattr("trusted_router.storage_gcp_io.time.monotonic", clock.monotonic)
    monkeypatch.setattr("trusted_router.storage_gcp_io.time.sleep", clock.sleep)


def test_run_in_transaction_with_retry_records_winning_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("trusted_router.storage_gcp_io.time.sleep", lambda _seconds: None)

    first_try = _RetryingDatabase(aborts_before_success=0)
    attempts_box: list[int] = []
    assert run_in_transaction_with_retry(first_try, _txn, attempts_out=attempts_box) == "ok"
    assert attempts_box == [1]
    assert first_try.calls == 1

    retried = _RetryingDatabase(aborts_before_success=2)
    attempts_box = []
    assert run_in_transaction_with_retry(retried, _txn, attempts=4, attempts_out=attempts_box) == "ok"
    assert attempts_box == [3]
    assert retried.calls == 3

    omitted = _RetryingDatabase(aborts_before_success=1)
    assert run_in_transaction_with_retry(omitted, _txn, attempts=3) == "ok"
    assert omitted.calls == 2


def test_run_in_transaction_with_retry_does_not_record_exhausted_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("trusted_router.storage_gcp_io.time.sleep", lambda _seconds: None)

    database = _RetryingDatabase(aborts_before_success=5)
    attempts_out: list[int] = []
    with pytest.raises(Aborted):
        run_in_transaction_with_retry(database, _txn, attempts=3, attempts_out=attempts_out)

    assert attempts_out == []


def test_passes_remaining_budget_as_inner_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first inner attempt is handed ~the full budget as timeout_secs."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)

    database = _RetryingDatabase(aborts_before_success=0)
    assert run_in_transaction_with_retry(database, _txn, total_budget_seconds=17.0) == "ok"
    assert database.timeouts == [pytest.approx(17.0)]


def test_default_budget_is_below_http_timeout() -> None:
    # The whole point of the cap: it must fail retryably before the upstream 30s
    # HTTP timeout turns the hang into an upstream 502.
    assert TXN_BUDGET_SECONDS < 30.0


class _TimedAbortingDatabase:
    """Aborts every attempt, advancing the clock to simulate a contended txn and
    recording the ``timeout_secs`` it was handed each call."""

    def __init__(self, clock: _Clock, attempt_cost: float) -> None:
        self.clock = clock
        self.attempt_cost = attempt_cost
        self.calls = 0
        self.timeouts: list[float | None] = []

    def run_in_transaction(
        self, func: Callable[..., str], *, timeout_secs: float | None = None
    ) -> str:
        self.calls += 1
        self.timeouts.append(timeout_secs)
        budget = timeout_secs if timeout_secs is not None else self.attempt_cost
        self.clock.now += min(self.attempt_cost, budget)
        raise Aborted("contended")


def test_budget_bounds_total_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)

    start = clock.now
    budget = 10.0
    attempt_cost = 3.0
    database = _TimedAbortingDatabase(clock, attempt_cost=attempt_cost)

    with pytest.raises(Aborted):
        # attempts far exceeds what the budget can fit — the wall-clock, not the
        # attempt count, must be what stops the loop.
        run_in_transaction_with_retry(
            database, _txn, attempts=1000, total_budget_seconds=budget
        )

    elapsed = clock.now - start
    # A single in-flight attempt may overrun the deadline by at most its own cost.
    assert elapsed <= budget + attempt_cost
    # Budget, not the 1000 attempt cap, terminated the loop.
    assert database.calls < 1000
    # Every inner attempt got a positive deadline that never exceeded the budget.
    assert database.timeouts
    assert all(t is not None and 0.0 < t <= budget + 1e-9 for t in database.timeouts)


def test_backoff_never_sleeps_past_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)

    start = clock.now
    budget = 5.0
    # attempt_cost 0 => all elapsed time comes from backoff sleeps; if backoff ever
    # slept past the deadline this would overrun.
    database = _TimedAbortingDatabase(clock, attempt_cost=0.0)

    with pytest.raises(Aborted):
        run_in_transaction_with_retry(
            database, _txn, attempts=1000, total_budget_seconds=budget
        )

    assert clock.now - start <= budget


def test_non_aborted_exceptions_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trusted_router.storage_gcp_io.time.sleep", lambda _seconds: None)

    class _Boom:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc
            self.calls = 0

        def run_in_transaction(
            self, func: Callable[..., str], *, timeout_secs: float | None = None
        ) -> str:
            self.calls += 1
            raise self.exc

    for exc in (ValueError("boom"), TypeError("bad signature"), RuntimeError("x")):
        database = _Boom(exc)
        with pytest.raises(type(exc)):
            run_in_transaction_with_retry(database, _txn, attempts=5)
        assert database.calls == 1


def test_aborted_retries_then_succeeds_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)

    database = _RetryingDatabase(aborts_before_success=3)
    attempts_box: list[int] = []
    assert (
        run_in_transaction_with_retry(
            database, _txn, attempts=8, attempts_out=attempts_box, total_budget_seconds=20.0
        )
        == "ok"
    )
    assert attempts_box == [4]
    assert database.calls == 4
    # Each attempt's inner deadline shrank as the shared budget was consumed by backoff.
    handed = [t for t in database.timeouts if t is not None]
    assert handed[0] == pytest.approx(20.0)
    assert all(0.0 < t <= 20.0 + 1e-9 for t in handed)
    assert handed == sorted(handed, reverse=True)
