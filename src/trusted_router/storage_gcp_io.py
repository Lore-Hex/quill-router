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

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


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


def run_in_transaction_with_retry(
    database: Any,
    func: Callable[..., T],
    *,
    attempts: int = 8,
    attempts_out: list[int] | None = None,
    total_budget_seconds: float = TXN_BUDGET_SECONDS,
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
            result = database.run_in_transaction(func, timeout_secs=inner_timeout)
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
