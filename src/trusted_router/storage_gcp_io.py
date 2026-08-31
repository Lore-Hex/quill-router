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

    Only ``Aborted`` is retried; every other callback exception (including
    ``TypeError``) propagates unchanged and is never mistaken for a signature
    mismatch. On budget exhaustion the final ``Aborted`` is raised so callers
    can map it to a retryable error rather than a multi-minute hang.

    ``attempts_out``, if given, receives the winning attempt number (1 = no
    retry) — used to attribute finalize latency to contention.

    ``transaction_tag`` is a stable, non-sensitive operation label forwarded
    to Spanner on every retry. It makes lock-stat samples attributable without
    placing workspace, key, request, or authorization identifiers in telemetry.
    """
    from google.api_core.exceptions import Aborted

    deadline = time.monotonic() + max(total_budget_seconds, _MIN_INNER_TIMEOUT_SECONDS)
    delay = 0.05
    last_aborted: Aborted | None = None
    for attempt in range(1, attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0 and last_aborted is not None:
            # Budget spent between the last abort's backoff and here.
            raise last_aborted
        # Cap the client's internal retry to what's left of our wall-clock so a
        # single attempt cannot outlive the caller's budget (min floor keeps a
        # valid positive deadline for the final sliver).
        inner_timeout = max(remaining, _MIN_INNER_TIMEOUT_SECONDS)
        try:
            transaction_kwargs: dict[str, Any] = {"timeout_secs": inner_timeout}
            if transaction_tag is not None:
                transaction_kwargs["transaction_tag"] = transaction_tag
            result = database.run_in_transaction(func, **transaction_kwargs)
        except Aborted as exc:
            last_aborted = exc
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
