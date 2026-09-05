"""Spanner IO adapter for SpannerBigtableStore feature classes.

The composed feature stores (SpannerWalletChallenges,
SpannerVerificationTokens, SpannerEmailBlocks) need a small set of Spanner
primitives — read/write/batch + transaction runner. Pulling them into a
typed adapter lets each feature class declare exactly what it depends on
without importing SpannerBigtableStore (which would be a cycle).

The adapter is a plain dataclass holding callables; SpannerBigtableStore
wires it up once in __init__ from its own bound methods. There's no logic
here, just plumbing.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar, cast

T = TypeVar("T")
P = ParamSpec("P")


# Total wall-clock budget for a retried transaction. Must sit safely BELOW the
# upstream HTTP client timeout (30s) so a contended hot-path txn fails RETRYABLY
# inside the caller's budget instead of hanging past it and surfacing as an
# upstream 502. Previously up to 8 outer attempts each carried the Spanner
# client's own ~30s internal retry deadline, with no timeout_secs ever passed
# inward — so sustained hot-row contention could stall a call for minutes. 20s
# leaves room for serialization + transit + an actionable error. Maintenance
# txns (grants/reconcile) normally finish in well under a second, so this cap
# does not affect them in practice; it only truncates the contended tail.
TXN_BUDGET_SECONDS = 20.0
_MIN_INNER_TIMEOUT_SECONDS = 0.5
# Floor for the best-effort Rollback RPC issued when a transaction callback
# fails with a deterministic API error. The failing statement usually spent the
# shared ContextVar budget, and ``configure_spanner_rpc_deadlines`` raises
# DeadlineExceeded for any RPC (rollback included) once that budget is gone —
# which would silently leave the server-side locks held for Spanner's idle
# reap (~16s) instead of releasing them now.
_ROLLBACK_FLOOR_SECONDS = 2.0
_SPANNER_RPC_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "trusted_router_spanner_rpc_deadline",
    default=None,
)


def spanner_rpc_budget(max_seconds: float) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Share one Spanner deadline across every transaction in a hot-path call."""
    if max_seconds <= 0:
        raise ValueError("Spanner RPC budget must be positive")

    def decorate(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def budgeted(*args: P.args, **kwargs: P.kwargs) -> T:
            deadline = time.monotonic() + max_seconds
            existing_deadline = _SPANNER_RPC_DEADLINE.get()
            if existing_deadline is not None:
                deadline = min(deadline, existing_deadline)
            token = _SPANNER_RPC_DEADLINE.set(deadline)
            try:
                return func(*args, **kwargs)
            finally:
                _SPANNER_RPC_DEADLINE.reset(token)

        return budgeted

    return decorate


def configure_spanner_rpc_deadlines(
    database: Any,
    *,
    max_seconds: float = TXN_BUDGET_SECONDS,
) -> None:
    """Apply a real wall-clock deadline to every Spanner transaction RPC.

    ``Database.run_in_transaction(timeout_secs=...)`` only limits the client's
    ABORTED retry loop. It does not propagate that budget to ``Commit`` or the
    statement RPCs. In google-cloud-spanner 3.65, Commit's generated default is
    one hour, so a wedged channel can outlive Cloud Run's five-minute request
    timeout even when ``timeout_secs`` is 20 seconds.

    The database wrapper establishes one deadline for the whole transaction.
    The API wrappers cap each RPC and its generated retry policy to the
    remaining budget. A ContextVar isolates concurrent request threads and also
    stops Transaction.commit's private RST_STREAM retry loop from receiving a
    fresh budget on every attempt.
    """
    if max_seconds <= 0:
        raise ValueError("Spanner RPC deadline must be positive")
    if getattr(database, "_trusted_router_rpc_deadlines", False):
        return

    from google.api_core import gapic_v1
    from google.api_core.exceptions import DeadlineExceeded

    api = database.spanner_api
    transport = getattr(api, "_transport", None)
    wrapped_methods = getattr(transport, "_wrapped_methods", {})

    def remaining_seconds() -> float:
        deadline = _SPANNER_RPC_DEADLINE.get()
        if deadline is None:
            return max_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded(
                "TrustedRouter Spanner transaction deadline exceeded"
            )
        return min(remaining, max_seconds)

    for method_name in (
        "begin_transaction",
        "commit",
        "execute_sql",
        "execute_streaming_sql",
        "read",
        "streaming_read",
        "rollback",
    ):
        original = getattr(api, method_name, None)
        if not callable(original):
            continue
        original_method = cast(Callable[..., Any], original)
        transport_method = getattr(transport, method_name, None)
        wrapped_method = wrapped_methods.get(transport_method)
        default_retry = getattr(wrapped_method, "_retry", None)

        @functools.wraps(original_method)
        def bounded_rpc(
            *args: Any,
            _original: Callable[..., Any] = original_method,
            _default_retry: Any = default_retry,
            **kwargs: Any,
        ) -> Any:
            remaining = remaining_seconds()
            requested_timeout = kwargs.get("timeout", gapic_v1.method.DEFAULT)
            if (
                requested_timeout is gapic_v1.method.DEFAULT
                or requested_timeout is None
            ):
                kwargs["timeout"] = remaining
            else:
                kwargs["timeout"] = min(float(requested_timeout), remaining)

            requested_retry = kwargs.get("retry", gapic_v1.method.DEFAULT)
            retry = (
                _default_retry
                if requested_retry is gapic_v1.method.DEFAULT
                else requested_retry
            )
            if retry is not None and hasattr(retry, "with_timeout"):
                kwargs["retry"] = retry.with_timeout(remaining)
            return _original(*args, **kwargs)

        setattr(api, method_name, bounded_rpc)

    original_run_in_transaction = database.run_in_transaction

    @functools.wraps(original_run_in_transaction)
    def bounded_run_in_transaction(
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        requested_budget = kwargs.get("timeout_secs")
        budget = (
            max_seconds
            if requested_budget is None
            else min(float(requested_budget), max_seconds)
        )
        budget = max(budget, _MIN_INNER_TIMEOUT_SECONDS)
        kwargs["timeout_secs"] = budget
        deadline = time.monotonic() + budget
        existing_deadline = _SPANNER_RPC_DEADLINE.get()
        if existing_deadline is not None:
            deadline = min(deadline, existing_deadline)
        token = _SPANNER_RPC_DEADLINE.set(deadline)
        try:
            return original_run_in_transaction(func, *args, **kwargs)
        finally:
            _SPANNER_RPC_DEADLINE.reset(token)

    database.run_in_transaction = bounded_run_in_transaction
    database._trusted_router_rpc_deadlines = True


def run_in_transaction_with_retry(
    database: Any,
    func: Callable[..., T],
    *,
    attempts: int = 8,
    attempts_out: list[int] | None = None,
    total_budget_seconds: float = TXN_BUDGET_SECONDS,
    transaction_tag: str | None = None,
    also_retry: tuple[type[BaseException], ...] = (),
) -> T:
    """Run a Spanner transaction, retrying on ABORTED within a wall-clock budget.

    Spanner already retries ABORTED to an internal deadline, but sustained
    hot-row contention — e.g. many concurrent /internal/gateway/authorize or
    settle calls for one high-QPS workspace all read-modify-writing its single
    tr_credit_balance row, or the per-key limit counters in storage_gcp_keys
    (reserve_key_limit / _release_limit / add_usage) — can exhaust that
    deadline and surface ``Aborted`` to the caller.

    ``total_budget_seconds`` bounds the ENTIRE retry loop (monotonic clock), and
    each ``database.run_in_transaction`` receives ``timeout_secs`` set to the
    remaining budget so the client's own internal retry can never run past it.
    Without this, up to ``attempts`` outer tries each carried a fresh ~30s inner
    deadline, producing multi-minute hangs past the upstream 30s HTTP timeout.

    Retrying is safe ONLY for idempotent transactions. Spanner already
    re-invokes ``func`` on its own internal retries, so callers already write
    `func` to tolerate re-execution AND guard their externally-visible side
    effects (e.g. settle's ``authorization.settled`` / ``reservation.settled``
    checks, credit's ``stripe_event`` idempotency row); this wrapper only adds
    more attempts of that same safe re-run. Do not pass a transaction whose
    callback performs a non-idempotent side effect. Exponential backoff with
    jitter de-synchronizes contenders so they stop lockstepping on the row.

    By default only ``Aborted`` is retried. ``also_retry`` lets one caller add
    its own typed callback conflict without teaching the Spanner client about
    that exception; every other callback exception (including ``TypeError``)
    propagates unchanged. On budget exhaustion the final retryable exception
    is raised so the caller can map it to its ordinary contention response.

    ``attempts_out``, if given, receives the winning attempt number (1 = no
    retry) — used to attribute finalize latency to contention.

    ``transaction_tag`` is a stable, non-sensitive operation label forwarded
    to Spanner on every retry. It makes lock-stat samples attributable without
    placing workspace, key, request, or authorization identifiers in telemetry.
    """
    from google.api_core.exceptions import Aborted

    retryable_errors = (Aborted,) + also_retry
    rolled_back_func = _rollback_on_api_error(func)
    deadline = time.monotonic() + max(total_budget_seconds, _MIN_INNER_TIMEOUT_SECONDS)
    delay = 0.05
    last_retryable: BaseException | None = None
    for attempt in range(1, attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0 and last_retryable is not None:
            # Budget spent between the last abort's backoff and here.
            raise last_retryable
        # Cap the client's internal retry to what's left of our wall-clock so a
        # single attempt cannot outlive the caller's budget (min floor keeps a
        # valid positive deadline for the final sliver).
        inner_timeout = max(remaining, _MIN_INNER_TIMEOUT_SECONDS)
        try:
            transaction_kwargs: dict[str, Any] = {"timeout_secs": inner_timeout}
            if transaction_tag is not None:
                transaction_kwargs["transaction_tag"] = transaction_tag
            result = database.run_in_transaction(rolled_back_func, **transaction_kwargs)
        except retryable_errors as exc:
            last_retryable = exc
            if attempt >= attempts:
                raise
            remaining_after = deadline - time.monotonic()
            if remaining_after <= _MIN_INNER_TIMEOUT_SECONDS:
                # No room for another attempt within budget — fail now.
                raise
            jitter = secrets.randbelow(1_000_000) / 1_000_000 * delay
            sleep_for = min(delay + jitter, remaining_after - _MIN_INNER_TIMEOUT_SECONDS)
            if sleep_for > 0:
                time.sleep(sleep_for)
            delay = min(delay * 2.0, 2.0)
            continue
        if attempts_out is not None:
            attempts_out.append(attempt)
        return result
    raise AssertionError("unreachable")  # pragma: no cover


def _rollback_on_api_error(func: Callable[..., T]) -> Callable[..., T]:
    """Roll back the server-side transaction when the callback fails with a
    non-Aborted ``GoogleAPICallError``.

    google-cloud-spanner's ``Session.run_in_transaction`` rolls back only on
    generic Python exceptions; on ``GoogleAPICallError`` (e.g. ``AlreadyExists``
    from a UNIQUE index, ``FailedPrecondition``) it drops its client-side
    handle and re-raises, and with multiplexed read-write sessions the next
    transaction on the session does not invalidate the orphan either. Its
    ReaderShared/Exclusive locks then outlive the caller by Spanner's idle
    reap (~16s), and every later writer of those rows queues behind them.
    ``Aborted`` is deliberately excluded: the client retries it and owns that
    transaction's lifecycle. Rollback is best-effort — the transaction is
    discarded either way — so its own failure never masks the real error.
    """
    from google.api_core.exceptions import Aborted, GoogleAPICallError

    @functools.wraps(func)
    def rolled_back(transaction: Any, *args: Any, **kwargs: Any) -> T:
        try:
            return func(transaction, *args, **kwargs)
        except Aborted:
            raise
        except GoogleAPICallError:
            _rollback_discarded_transaction(transaction)
            raise

    return rolled_back


def _rollback_discarded_transaction(transaction: Any) -> None:
    rollback = getattr(transaction, "rollback", None)
    if not callable(rollback):
        return
    # Independent floor for the Rollback RPC: the failing statement typically
    # exhausted the shared ContextVar budget, and the bounded RPC wrappers
    # would otherwise raise DeadlineExceeded before the request is even sent.
    floor = time.monotonic() + _ROLLBACK_FLOOR_SECONDS
    existing_deadline = _SPANNER_RPC_DEADLINE.get()
    token = None
    if existing_deadline is not None and existing_deadline < floor:
        token = _SPANNER_RPC_DEADLINE.set(floor)
    try:
        # Best effort: the transaction is discarded either way and the original
        # API error is what propagates to the caller.
        with contextlib.suppress(Exception):
            rollback()
    finally:
        if token is not None:
            _SPANNER_RPC_DEADLINE.reset(token)


@dataclass(frozen=True)
class SpannerIO:
    database: Any
    spanner_module: Any
    write_entity_batch: Callable[[Any, str, str, Any], None]
    read_entity_tx: Callable[[Any, str, str, type], Any]
    write_entity_tx: Callable[[Any, str, str, Any], None]
    write_entity: Callable[[str, str, Any], None]
    read_entity: Callable[[str, str, type], Any]
    list_entities: Callable[..., list[Any]]
    delete_entities: Callable[[str, list[str]], None]
    delete_entities_tx: Callable[[Any, str, list[str]], None]
    # Optional for feature adapters that never issue typed SQL. Production and
    # the user-model slot adapter always provide the real param-types module.
    param_types: Any = None
