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


class DeferredSettlementCapReached(StoreError):
    """This plane already holds the maximum unsettled spend for a workspace.

    A refusal, not a failure: the request is admissible again as soon as
    settlements are delivered to the home plane (or the reaper reclaims an
    abandoned authorization). Distinct from insufficient credits, which is a
    statement about the customer's money; this one is a statement about how
    much debt THIS plane is willing to carry on their behalf while the home
    plane is unreachable.

    A StoreError rather than a bare ValueError because the routes must be able
    to tell it apart from "the local balance refused" without matching on
    message text — the two lead to opposite responses.
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
def _postgres_error_types() -> tuple[tuple[type[Exception], ...], tuple[type[Exception], ...]]:
    """``(transient, conflict)`` types contributed by psycopg.

    Psycopg is optional for non-Postgres deployments, so this follows the
    Google adapter's lazy-import boundary.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - only hit without Postgres deps
        return ((), ())

    transient: tuple[type[Exception], ...] = (
        psycopg.InterfaceError,
        psycopg.OperationalError,
    )
    conflict: tuple[type[Exception], ...] = (
        psycopg.errors.SerializationFailure,
    )
    return (transient, conflict)


@lru_cache(maxsize=1)
def transient_store_error_types() -> tuple[type[Exception], ...]:
    """Types meaning "the write did not land; try again later".

    Returned as a tuple so it can be used directly in an `except` clause,
    which is how the settle-outbox drain consumes it.
    """
    google_transient, _ = _google_error_types()
    postgres_transient, _ = _postgres_error_types()
    return (
        StoreConflict,
        StoreUnavailable,
        *google_transient,
        *postgres_transient,
    )


@lru_cache(maxsize=1)
def conflict_store_error_types() -> tuple[type[Exception], ...]:
    """Types meaning "a concurrent writer aborted this; replaying may work"."""
    _, google_conflict = _google_error_types()
    _, postgres_conflict = _postgres_error_types()
    return (StoreConflict, *google_conflict, *postgres_conflict)


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


@lru_cache(maxsize=1)
def duplicate_key_store_error_types() -> tuple[type[Exception], ...]:
    """Types meaning "that primary key is already taken".

    The THIRD question application code asks about a storage failure, and the
    one insert-once idempotency is built on: a losing INSERT must be told apart
    from a conflict (replay the transaction) and from a transient fault (retry
    later), because the correct response is neither — it is to go and read what
    the winner wrote.

    Deliberately NOT folded into `conflict_store_error_types`: a conflict means
    nothing committed and re-running is right, whereas a duplicate key means
    somebody else's write DID commit and re-running would fail identically
    forever. Retrying a duplicate key is an infinite loop; replaying a conflict
    is the fix.
    """
    types: list[type[Exception]] = []
    try:
        from google.api_core.exceptions import AlreadyExists
    except ImportError:  # pragma: no cover - only hit without GCP deps
        pass
    else:
        types.append(AlreadyExists)
    try:
        import psycopg
    except ImportError:  # pragma: no cover - only hit without Postgres deps
        pass
    else:
        types.append(psycopg.errors.UniqueViolation)
    return tuple(types)


def is_duplicate_key_error(exc: BaseException) -> bool:
    """True when an insert lost an insert-once race on an existing key."""
    if isinstance(exc, duplicate_key_store_error_types()):
        return True
    # Name-based fallback for the in-process Spanner fake when the Google
    # libraries are absent: `FakeAlreadyExists` subclasses the real type when it
    # can import it and plain `Exception` when it cannot, and in the latter case
    # there is no type left to match on.
    return type(exc).__name__ in ("AlreadyExists", "FakeAlreadyExists")
