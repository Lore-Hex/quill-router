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


def run_in_transaction_with_retry(database: Any, func: Callable[..., T], *, attempts: int = 8) -> T:
    """Run a Spanner transaction, retrying on ABORTED with backoff + jitter.

    Spanner already retries ABORTED to an internal deadline, but sustained
    hot-row contention — e.g. many concurrent /internal/gateway/settle calls
    for one high-QPS api_key all read-modify-writing its usage/reserved
    counters, or the per-key limit counters in storage_gcp_keys
    (reserve_key_limit / _release_limit / add_usage) — can exhaust that
    deadline and surface ``Aborted: Transaction was aborted`` to the caller
    (observed on finalize_gateway_authorization, 2026-06-19).

    Retrying is safe ONLY for idempotent transactions. Spanner already
    re-invokes ``func`` on its own internal retries, so callers already write
    `func` to tolerate re-execution AND guard their externally-visible side
    effects (e.g. settle's ``authorization.settled`` / ``reservation.settled``
    checks, credit's ``stripe_event`` idempotency row); this wrapper only adds
    more attempts of that same safe re-run. Do not pass a transaction whose
    callback performs a non-idempotent side effect. Exponential backoff with
    jitter de-synchronizes contenders so they stop lockstepping on the row.

    Unlike the rate limiter (middleware._enforce_rate_limit), which fails OPEN
    on this same contention, billing transactions must not be dropped — so we
    retry rather than swallow the abort.
    """
    from google.api_core.exceptions import Aborted

    delay = 0.05
    for attempt in range(1, attempts + 1):
        try:
            return database.run_in_transaction(func)
        except Aborted:
            if attempt >= attempts:
                raise
            jitter = secrets.randbelow(1_000_000) / 1_000_000 * delay
            time.sleep(delay + jitter)
            delay = min(delay * 2.0, 2.0)
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True)
class SpannerIO:
    database: Any
    write_entity_batch: Callable[[Any, str, str, Any], None]
    read_entity_tx: Callable[[Any, str, str, type], Any]
    write_entity_tx: Callable[[Any, str, str, Any], None]
    write_entity: Callable[[str, str, Any], None]
    read_entity: Callable[[str, str, type], Any]
    list_entities: Callable[..., list[Any]]
    delete_entities: Callable[[str, list[str]], None]
    delete_entities_tx: Callable[[Any, str, list[str]], None]
