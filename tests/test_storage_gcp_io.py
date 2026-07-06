from __future__ import annotations

from collections.abc import Callable

import pytest
from google.api_core.exceptions import Aborted

from trusted_router.storage_gcp_io import run_in_transaction_with_retry


class _RetryingDatabase:
    def __init__(self, aborts_before_success: int) -> None:
        self.aborts_before_success = aborts_before_success
        self.calls = 0

    def run_in_transaction(self, func: Callable[..., str]) -> str:
        self.calls += 1
        if self.calls <= self.aborts_before_success:
            raise Aborted("spanner aborted")
        return func("txn")


def _txn(_transaction: object) -> str:
    return "ok"


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
