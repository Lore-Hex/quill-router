"""Cold-path escrow maintenance for exact API-key lifetime limits."""

from __future__ import annotations

import logging
from typing import Any

from trusted_router.storage_gcp_counters import (
    KEY_LIMIT_TABLE,
    distribute_credit_amount,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry

log = logging.getLogger(__name__)


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

    The accepted hot path never calls this function. It runs only after every
    shard rejected an exact-cap reservation, which can mean either genuine
    exhaustion or harmless escrow fragmentation. A strong snapshot rejects
    exhaustion without write-transaction locks. A transaction then rereads
    every shard and moves allowance without changing the sum of row limits.
    """
    if shard_count < 2:
        return False
    if preferred_shard < 0 or preferred_shard >= shard_count:
        raise ValueError("preferred key escrow shard is outside the configured set")
    required = int(estimate)
    if required <= 0:
        return False
    pt = param_types

    def read_counters(reader: Any) -> tuple[list[int], list[int]] | None:
        rows = list(
            reader.execute_sql(
                "SELECT shard, limit_micro, usage, byok_usage, reserved, include_byok "
                "FROM tr_key_limit WHERE key_hash=@kh AND shard>=0 "
                "AND shard<@shard_count ORDER BY shard",
                params={"kh": key_hash, "shard_count": shard_count},
                param_types={"kh": pt.STRING, "shard_count": pt.INT64},
            )
        )
        if [int(row[0]) for row in rows] != list(range(shard_count)):
            raise RuntimeError("configured tr_key_limit usage shard set is incomplete")
        if any(row[1] is None for row in rows):
            return None  # uncapped or inconsistent config; no exact escrow to move
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
        return [int(row[1]) for row in rows], consumed

    # An exhausted key needs no repair. A strong snapshot avoids taking shared
    # locks on every allowance row while concurrent settlements release holds.
    # A positive observation is only a hint: all counters are reread in the RW
    # transaction below before moving any allowance.
    try:
        with database.snapshot() as snapshot:
            observed = read_counters(snapshot)
    except Exception as exc:
        # An optimization cannot make the original transaction unavailable.
        # The transaction still enforces every invariant and the RPC budget.
        log.warning("key_escrow.snapshot_failed error_class=%s", type(exc).__name__)
    else:
        if observed is None or sum(observed[0]) - sum(observed[1]) < required:
            return False

    def txn(transaction: Any) -> bool:
        current = read_counters(transaction)
        if current is None:
            return False
        old_limits, consumed = current
        remaining = sum(old_limits) - sum(consumed)
        if remaining < required:
            return False

        headroom = list(distribute_credit_amount(remaining - required, shard_count))
        headroom[preferred_shard] += required
        limits = [
            current + extra
            for current, extra in zip(consumed, headroom, strict=True)
        ]
        changed = [
            (key_hash, shard, limits[shard])
            for shard in range(shard_count)
            if limits[shard] != old_limits[shard]
        ]
        if changed:
            # Every row was proved present in this transaction. UPDATE limits
            # the mutation to allowance; the incident's INSERT_OR_UPDATE lock
            # samples also included omitted, hot settlement counter columns.
            transaction.update(
                table=KEY_LIMIT_TABLE,
                columns=("key_hash", "shard", "limit_micro"),
                values=changed,
            )
        return True

    return bool(run_in_transaction_with_retry(database, txn))
