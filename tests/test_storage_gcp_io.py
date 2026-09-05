from __future__ import annotations

from collections.abc import Callable

import pytest
from google.api_core.exceptions import (
    Aborted,
    AlreadyExists,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    ServiceUnavailable,
)
from google.api_core.retry import Retry, if_exception_type

from tests.fakes.spanner import FakeAborted, FakeAlreadyExists
from trusted_router import storage_gcp_io as io_mod
from trusted_router.storage_gcp_io import (
    TXN_BUDGET_SECONDS,
    configure_spanner_rpc_deadlines,
    run_in_transaction_with_retry,
    spanner_rpc_budget,
)


class _RetryingDatabase:
    def __init__(self, aborts_before_success: int) -> None:
        self.aborts_before_success = aborts_before_success
        self.calls = 0
        self.timeouts: list[float | None] = []
        self.transaction_tags: list[str | None] = []

    def run_in_transaction(
        self,
        func: Callable[..., str],
        *,
        timeout_secs: float | None = None,
        transaction_tag: str | None = None,
    ) -> str:
        self.calls += 1
        self.timeouts.append(timeout_secs)
        self.transaction_tags.append(transaction_tag)
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


def test_run_in_transaction_with_retry_forwards_stable_tag_to_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("trusted_router.storage_gcp_io.time.sleep", lambda _seconds: None)

    database = _RetryingDatabase(aborts_before_success=2)
    assert (
        run_in_transaction_with_retry(
            database,
            _txn,
            attempts=4,
            transaction_tag="tr_authorize",
        )
        == "ok"
    )
    assert database.transaction_tags == ["tr_authorize"] * 3


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


class _RollbackTrackingTransaction:
    def __init__(self, *, rollback_error: Exception | None = None) -> None:
        self.rollbacks = 0
        self.rollback_deadlines: list[float | None] = []
        self._rollback_error = rollback_error

    def rollback(self) -> None:
        self.rollbacks += 1
        self.rollback_deadlines.append(io_mod._SPANNER_RPC_DEADLINE.get())
        if self._rollback_error is not None:
            raise self._rollback_error


class _CallbackDatabase:
    """Hands the callback a transaction the way ``Session.run_in_transaction``
    does, and — like both the real client and the test fake — re-invokes the
    callback on an abort. It never rolls back itself: the real client skips
    rollback on GoogleAPICallError, which is exactly the gap under test."""

    def __init__(self, *, rollback_error: Exception | None = None) -> None:
        self.calls = 0
        self.transactions: list[_RollbackTrackingTransaction] = []
        self._rollback_error = rollback_error

    def run_in_transaction(
        self,
        func: Callable[..., str],
        *,
        timeout_secs: float | None = None,
        transaction_tag: str | None = None,
    ) -> str:
        while True:
            self.calls += 1
            txn = _RollbackTrackingTransaction(rollback_error=self._rollback_error)
            self.transactions.append(txn)
            try:
                return func(txn)
            except (FakeAborted, Aborted):
                continue


def test_api_call_error_rolls_back_the_transaction_exactly_once() -> None:
    boom = FakeAlreadyExists("tr_gateway_authorization_by_gateway_request_id")

    def failing(_transaction: object) -> str:
        raise boom

    database = _CallbackDatabase()
    with pytest.raises(AlreadyExists) as excinfo:
        run_in_transaction_with_retry(database, failing, attempts=5)

    assert excinfo.value is boom, "the API error must propagate unchanged"
    assert database.calls == 1, "a deterministic API error is never retried"
    [txn] = database.transactions
    assert txn.rollbacks == 1


def test_rollback_failure_never_masks_the_original_api_error() -> None:
    boom = FakeAlreadyExists("duplicate")

    def failing(_transaction: object) -> str:
        raise boom

    # Real Transaction.rollback raises ValueError when the transaction was
    # never begun; a best-effort rollback must swallow that and re-raise boom.
    database = _CallbackDatabase(rollback_error=ValueError("Transaction is not begun"))
    with pytest.raises(AlreadyExists) as excinfo:
        run_in_transaction_with_retry(database, failing, attempts=5)
    assert excinfo.value is boom
    assert database.transactions[0].rollbacks == 1


@pytest.mark.parametrize("abort", [FakeAborted("fake abort"), Aborted("spanner aborted")])
def test_aborted_is_left_to_the_client_retry_without_rollback(abort: Exception) -> None:
    outcomes = iter([abort, None])

    def flaky(_transaction: object) -> str:
        exc = next(outcomes)
        if exc is not None:
            raise exc
        return "ok"

    database = _CallbackDatabase()
    assert run_in_transaction_with_retry(database, flaky, attempts=5) == "ok"
    assert database.calls == 2, "the client-side retry re-ran the callback"
    assert [txn.rollbacks for txn in database.transactions] == [0, 0]


def test_rollback_gets_a_deadline_floor_after_the_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing statement usually exhausted the shared RPC budget; the
    bounded rollback RPC must still get a positive deadline or the locks stay
    held for Spanner's idle reap."""
    clock = _Clock()
    _install_clock(monkeypatch, clock)

    def failing(_transaction: object) -> str:
        raise FakeAlreadyExists("duplicate")

    database = _CallbackDatabase()
    spent = clock.now - 1.0
    token = io_mod._SPANNER_RPC_DEADLINE.set(spent)
    try:
        with pytest.raises(AlreadyExists):
            run_in_transaction_with_retry(database, failing, attempts=5)
        # The floor is scoped to the rollback RPC; the caller's budget is untouched.
        assert io_mod._SPANNER_RPC_DEADLINE.get() == spent
    finally:
        io_mod._SPANNER_RPC_DEADLINE.reset(token)
    [txn] = database.transactions
    [deadline] = txn.rollback_deadlines
    assert deadline is not None
    assert deadline >= clock.now + io_mod._ROLLBACK_FLOOR_SECONDS - 1e-9


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


class _WrappedRpc:
    def __init__(self) -> None:
        self._retry = Retry(
            predicate=if_exception_type(ResourceExhausted, ServiceUnavailable),
            timeout=3600.0,
        )


class _CommitTransport:
    def __init__(self) -> None:
        self.commit = object()
        self._wrapped_methods = {self.commit: _WrappedRpc()}


class _CommitApi:
    def __init__(self, clock: _Clock | None = None, *, fail: bool = False) -> None:
        self._transport = _CommitTransport()
        self.clock = clock
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def commit(self, *args: object, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        if self.clock is not None:
            self.clock.now += 6.0
        if self.fail:
            raise InternalServerError("RST_STREAM")
        return "committed"


class _CommitDatabase:
    def __init__(self, api: _CommitApi, *, retry_commit: bool = False) -> None:
        self.spanner_api = api
        self.retry_commit = retry_commit
        self.timeouts: list[float | None] = []

    def run_in_transaction(
        self,
        func: Callable[..., str],
        *,
        timeout_secs: float | None = None,
    ) -> str:
        self.timeouts.append(timeout_secs)
        result = func("txn")
        try:
            self.spanner_api.commit(request="commit")
        except InternalServerError:
            if not self.retry_commit:
                raise
            # Model Transaction.commit's private RST_STREAM retry. The second
            # call must inherit the original transaction deadline.
            self.spanner_api.commit(request="commit")
        return result


def test_configure_spanner_rpc_deadlines_caps_commit_and_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    database = _CommitDatabase(_CommitApi())
    configure_spanner_rpc_deadlines(database, max_seconds=7.0)

    assert database.run_in_transaction(_txn, timeout_secs=100.0) == "ok"
    assert database.timeouts == [pytest.approx(7.0)]
    commit_call = database.spanner_api.calls[0]
    assert commit_call["timeout"] == pytest.approx(7.0, abs=0.001)
    retry = commit_call["retry"]
    assert isinstance(retry, Retry)
    assert retry._timeout == pytest.approx(7.0, abs=0.001)


def test_commit_rst_retry_cannot_receive_a_fresh_transaction_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    api = _CommitApi(clock, fail=True)
    database = _CommitDatabase(api, retry_commit=True)
    configure_spanner_rpc_deadlines(database, max_seconds=5.0)

    with pytest.raises(DeadlineExceeded, match="transaction deadline exceeded"):
        database.run_in_transaction(_txn)

    # The first stuck commit consumes the budget. The retry is rejected before
    # reaching the transport instead of starting another one-hour default RPC.
    assert len(api.calls) == 1


def test_hot_path_budget_is_shared_across_multiple_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    _install_clock(monkeypatch, clock)
    api = _CommitApi(clock)
    database = _CommitDatabase(api)
    configure_spanner_rpc_deadlines(database, max_seconds=20.0)

    @spanner_rpc_budget(10.0)
    def two_transactions() -> None:
        database.run_in_transaction(_txn)
        database.run_in_transaction(_txn)

    two_transactions()

    assert len(api.calls) == 2
    assert api.calls[0]["timeout"] == pytest.approx(10.0)
    assert api.calls[1]["timeout"] == pytest.approx(4.0)
