"""Admission bounds for opt-in, single-counter strict window budgets."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from trusted_router.services.keyed_admission import KeyedConcurrencyAdmission
from trusted_router.storage_errors import StoreUnavailable

# Never queue strict requests in process memory. Fleet-wide correctness comes
# from conditional database updates, not this per-process harm limiter.
STRICT_ADMISSION = KeyedConcurrencyAdmission(max_subjects=16)
STRICT_BUDGET_SECONDS = 5.0


class StrictBudgetBusy(StoreUnavailable):
    pass


@contextmanager
def strict_budget_slot(key_hash: str) -> Iterator[float]:
    if not STRICT_ADMISSION.try_acquire(key_hash, limit=1):
        raise StrictBudgetBusy("Strict budget authorization is busy; retry with backoff")
    try:
        yield time.monotonic() + STRICT_BUDGET_SECONDS
    finally:
        STRICT_ADMISSION.release(key_hash)
