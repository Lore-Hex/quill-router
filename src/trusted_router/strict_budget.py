"""Admission bounds for opt-in, single-counter strict window budgets."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore

from trusted_router.services.keyed_admission import KeyedConcurrencyAdmission
from trusted_router.storage_errors import StoreUnavailable

# Bound both active subjects and waiting callers. Fleet-wide correctness comes
# from conditional database updates, not this per-process harm limiter.
STRICT_ADMISSION = KeyedConcurrencyAdmission(max_subjects=16)
STRICT_WAITERS = KeyedConcurrencyAdmission(max_subjects=16)
STRICT_WAITER_CAPACITY = BoundedSemaphore(16)
STRICT_ADMISSION_WAIT_SECONDS = 0.25
STRICT_ADMISSION_POLL_SECONDS = 0.025
STRICT_BUDGET_SECONDS = 5.0


class StrictBudgetBusy(StoreUnavailable):
    pass


def _wait_for_slot(key_hash: str, deadline: float) -> None:
    message = "Strict budget authorization is busy; retry with backoff"
    if not STRICT_WAITER_CAPACITY.acquire(blocking=False):
        raise StrictBudgetBusy(message)
    try:
        if not STRICT_WAITERS.try_acquire(key_hash, limit=2):
            raise StrictBudgetBusy(message)
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                if STRICT_ADMISSION.try_acquire(key_hash, limit=1):
                    return
                time.sleep(min(STRICT_ADMISSION_POLL_SECONDS, remaining))
            raise StrictBudgetBusy(message)
        finally:
            STRICT_WAITERS.release(key_hash)
    finally:
        STRICT_WAITER_CAPACITY.release()


@contextmanager
def strict_budget_slot(key_hash: str) -> Iterator[float]:
    started = time.monotonic()
    deadline = started + STRICT_BUDGET_SECONDS
    if not STRICT_ADMISSION.try_acquire(key_hash, limit=1):
        # Retry only admission, never a transaction with an uncertain outcome.
        _wait_for_slot(key_hash, min(deadline, started + STRICT_ADMISSION_WAIT_SECONDS))
    try:
        yield deadline
    finally:
        STRICT_ADMISSION.release(key_hash)
