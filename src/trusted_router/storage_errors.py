"""Backend-neutral storage error taxonomy.

Application code needs two answers about a storage failure: "is retrying
worthwhile?" and "did a concurrent writer abort this?". Today it answers by
importing `google.api_core.exceptions` directly — `main.py` registers an
exception handler on Spanner's `Aborted`, and the settle-outbox drain switches
on a tuple of six Google types. Neither decision is really about Google.

This module is the one place that knows how a concrete backend spells those
conditions. Callers ask `transient_store_error_types()` /
`conflict_store_error_types()` (or the `is_*` predicates) and stay portable;
teaching the system about Postgres means adding one mapping function here, not
editing call sites.

Two deliberate properties:

* **Google is imported lazily.** Importing `trusted_router.main` currently
  hard-requires `google-api-core`. A non-GCP deployment should not need the
  Google client libraries installed at all, so the import sits behind a cached
  helper that degrades to "no Google types" when they are absent.
* **Semantics are preserved exactly.** The transient set is the same six types
  the drain already parks on, and `Aborted` remains the sole conflict type.
  This is a refactor; the park-vs-error policy keys off exactly what it did
  before.
"""

from __future__ import annotations

from functools import lru_cache


class StoreError(Exception):
    """Base for backend-neutral storage failures."""


class StoreConflict(StoreError):
    """A concurrent writer won; the transaction was aborted.

    Spanner raises `Aborted`; Postgres raises serialization_failure (40001);
    CockroachDB raises a retry error; Aurora DSQL aborts under OCC. All mean
    the same thing — nothing committed, and replaying the whole transaction is
    the correct response.
    """


class StoreUnavailable(StoreError):
    """The backend could not service the request; retry later.

    Covers unavailability, deadline exhaustion, and admission-control or
    overload rejection. Distinct from `StoreConflict` because there is no
    useful *immediate* retry — the caller should back off or park the work.
    """


@lru_cache(maxsize=1)
def _google_error_types() -> tuple[tuple[type[Exception], ...], tuple[type[Exception], ...]]:
    """`(transient, conflict)` types contributed by the GCP backend.

    Empty when the Google libraries are not installed, which is the expected
    state on a non-GCP deployment.
    """
    try:
        from google.api_core.exceptions import (
            Aborted,
            DeadlineExceeded,
            InternalServerError,
            ResourceExhausted,
            RetryError,
            ServiceUnavailable,
        )
    except ImportError:  # pragma: no cover - only hit without GCP deps
        return ((), ())

    # The same six the settle-outbox drain treats as retryable.
    # ResourceExhausted covers Spanner session-pool / admission-control
    # overload. RetryError subclasses GoogleAPIError rather than
    # GoogleAPICallError, so it must be listed explicitly.
    transient: tuple[type[Exception], ...] = (
        Aborted,
        DeadlineExceeded,
        InternalServerError,
        ResourceExhausted,
        RetryError,
        ServiceUnavailable,
    )
    # Aborted is both transient AND a conflict: it is the one member of the set
    # meaning "a concurrent writer won" rather than "the backend is unwell",
    # and callers that retry immediately key off exactly that distinction.
    conflict: tuple[type[Exception], ...] = (Aborted,)
    return (transient, conflict)


@lru_cache(maxsize=1)
def transient_store_error_types() -> tuple[type[Exception], ...]:
    """Types meaning "the write did not land; try again later".

    Returned as a tuple so it can be used directly in an `except` clause,
    which is how the settle-outbox drain consumes it.
    """
    google_transient, _ = _google_error_types()
    return (StoreConflict, StoreUnavailable, *google_transient)


@lru_cache(maxsize=1)
def conflict_store_error_types() -> tuple[type[Exception], ...]:
    """Types meaning "a concurrent writer aborted this; replaying may work"."""
    _, google_conflict = _google_error_types()
    return (StoreConflict, *google_conflict)


def is_transient_store_error(exc: BaseException) -> bool:
    """True when retrying the operation later could succeed.

    Anything unrecognised is a bug rather than an infrastructure blip and
    should propagate to the caller's generic handler instead of being retried
    forever.
    """
    return isinstance(exc, transient_store_error_types())


def is_conflict_error(exc: BaseException) -> bool:
    """True when the transaction lost a write conflict and may be replayed."""
    return isinstance(exc, conflict_store_error_types())
