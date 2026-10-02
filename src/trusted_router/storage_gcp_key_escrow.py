"""Cold-path escrow maintenance for exact API-key lifetime limits.

A key with N usage shards splits its lifetime cap into N escrow sub-budgets
(``partition_key_limit``). Authorize reserves from one sub-budget, trying a
bounded, randomized prefix of the shards in its transaction. When that prefix
refuses the hold, the lock-free ``key_headroom_precheck`` decides what the
refusal means before anything takes a write lock: another shard can hold it
(retry that exact shard), only the pooled allowance can (move escrow with
``rebalance_key_limit_headroom``), or nothing can (deny).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from trusted_router.storage_gcp_counters import (
    KEY_LIMIT_TABLE,
    distribute_credit_amount,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry

log = logging.getLogger(__name__)


def key_escrow_rows(
    reader: Any, param_types: Any, *, key_hash: str, shard_count: int,
) -> list[list[Any]]:
    """The configured shards' escrow columns, in shard order.

    One read for every escrow decision: the lifetime-cap precheck and the
    headroom precheck read it on a snapshot; the rebalance reads it strongly
    inside its read-write transaction. Columns: shard, limit_micro, usage,
    byok_usage, reserved, include_byok.
    """
    pt = param_types
    return list(
        reader.execute_sql(
            "SELECT shard, limit_micro, usage, byok_usage, reserved, include_byok "
            "FROM tr_key_limit WHERE key_hash=@kh AND shard>=0 "
            "AND shard<@shard_count ORDER BY shard",
            params={"kh": key_hash, "shard_count": shard_count},
            param_types={"kh": pt.STRING, "shard_count": pt.INT64},
        )
    )


@dataclass(frozen=True)
class KeyHeadroom:
    """What a refused key hold means, read without locks.

    ``decisive=False``: the rows prove nothing (unreadable, an incomplete shard
    set, an uncapped or BYOK-excluded cap the transaction would not have
    enforced, mixed ``include_byok``); defer to the authoritative read-write
    rebalance, as before this precheck existed. Otherwise ``funded_shard`` is a
    shard whose own escrow covers the estimate (the one with the most
    headroom), and ``aggregate_covers`` says the pooled remaining allowance
    does. Neither means the cap is genuinely exhausted.
    """

    decisive: bool
    funded_shard: int | None = None
    aggregate_covers: bool = False


def key_headroom_precheck(
    database: Any,
    param_types: Any,
    *,
    key_hash: str,
    shard_count: int,
    estimate: int,
    has_credit_candidate: bool,
) -> KeyHeadroom:
    """Classify a refused exact-cap hold on one strong snapshot, without locks.

    The arithmetic is ``reserve_key``'s: headroom = limit - usage -
    (byok_usage if include_byok) - reserved, per shard. The pooled allowance is
    the SUM over every configured shard, as ``rebalance_key_limit_headroom``
    proves it under lock. Concurrent settles can only add headroom after this
    read, so a decisive "nothing covers it" is as authoritative as the
    transaction's own refusal.
    """
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    try:
        with database.snapshot() as snapshot:
            rows = key_escrow_rows(
                snapshot, param_types, key_hash=key_hash, shard_count=shard_count,
            )
    except Exception:
        log.warning(
            "key headroom snapshot failed; deferring to the escrow rebalance key=%s",
            key_hash,
            exc_info=True,
        )
        return KeyHeadroom(decisive=False)
    if [int(row[0]) for row in rows] != list(range(shard_count)):
        return KeyHeadroom(decisive=False)
    if any(row[1] is None for row in rows):
        return KeyHeadroom(decisive=False)
    include_values = {bool(row[5]) for row in rows}
    if len(include_values) != 1:
        return KeyHeadroom(decisive=False)
    include_byok = include_values.pop()
    if not has_credit_candidate and not include_byok:
        return KeyHeadroom(decisive=False)
    headroom = [
        int(limit_micro) - int(usage) - (int(byok_usage) if include_byok else 0) - int(reserved)
        for _shard, limit_micro, usage, byok_usage, reserved, _include in rows
    ]
    best = max(range(shard_count), key=lambda shard: headroom[shard])
    return KeyHeadroom(
        decisive=True,
        funded_shard=best if headroom[best] >= estimate else None,
        aggregate_covers=sum(headroom) >= estimate,
    )


def rebalance_key_limit_headroom(
    database: Any,
    param_types: Any,
    *,
    key_hash: str,
    shard_count: int,
    estimate: int,
    preferred_shard: int,
) -> bool:
    """Give one shard enough escrow for a large otherwise-affordable hold.

    The accepted hot path never calls this function. It runs only after a
    bounded authorize attempt refused an exact-cap reservation and the
    lock-free precheck found the pooled allowance sufficient (or could not
    decide). One transaction strongly reads every shard, proves the global
    remaining allowance, and moves that allowance without changing the sum of
    row limits.
    """
    if shard_count < 2:
        return False
    if preferred_shard < 0 or preferred_shard >= shard_count:
        raise ValueError("preferred key escrow shard is outside the configured set")
    required = int(estimate)
    if required <= 0:
        return False
    pt = param_types

    def txn(transaction: Any) -> bool:
        rows = key_escrow_rows(transaction, pt, key_hash=key_hash, shard_count=shard_count)
        if [int(row[0]) for row in rows] != list(range(shard_count)):
            raise RuntimeError("configured tr_key_limit usage shard set is incomplete")
        if any(row[1] is None for row in rows):
            return False  # uncapped or inconsistent config; no exact escrow to move
        include_values = {bool(row[5]) for row in rows}
        if len(include_values) != 1:
            raise RuntimeError("configured tr_key_limit include_byok values are inconsistent")
        include_byok = include_values.pop()
        counters = [
            (int(row[2]), int(row[3]), int(row[4]))
            for row in rows
        ]
        if any(value < 0 for current in counters for value in current):
            raise RuntimeError("key escrow counters must not be negative")
        consumed = [
            usage + (byok_usage if include_byok else 0) + reserved
            for usage, byok_usage, reserved in counters
        ]
        global_limit = sum(int(row[1]) for row in rows)
        remaining = global_limit - sum(consumed)
        if remaining < required:
            return False

        headroom = list(distribute_credit_amount(remaining - required, shard_count))
        headroom[preferred_shard] += required
        limits = [
            current + extra
            for current, extra in zip(consumed, headroom, strict=True)
        ]
        transaction.insert_or_update(
            table=KEY_LIMIT_TABLE,
            columns=("key_hash", "shard", "limit_micro"),
            values=[
                (key_hash, shard, limits[shard])
                for shard in range(shard_count)
            ],
        )
        return True

    return bool(run_in_transaction_with_retry(database, txn))
