"""Step 3: conditional-DML enforcement on the typed counter tables.

See docs/design/billing-typed-counters.md.

This is the deadlock fix. Instead of read-modify-write (a shared read lock that
upgrades to exclusive -> wound-wait deadlock under per-tenant concurrency), each
reserve is a SINGLE conditional UPDATE that takes the row write lock directly and
atomically checks-and-decrements:

    UPDATE tr_credit_balance SET reserved = reserved + @est
     WHERE workspace_id=@ws AND shard=@shard
       AND (total_credits - total_usage - reserved) >= @est

``execute_update()`` returns the modified-row count: 1 = accepted, 0 = rejected
(insufficient). Concurrent reservers serialize on the row write lock instead of
deadlocking; the predicate re-evaluates against committed state.

Standard DML only (NOT partitioned DML); these run inside ``run_in_transaction``
so ABORTED is retried. They must NOT be mixed with Spanner mutations in the same
transaction (docs §5) — the authorize/settle transactions are DML-only.
"""

from __future__ import annotations

from typing import Any

from trusted_router.storage_gcp_counters import UNSHARDED


def reserve_credit(
    transaction: Any, param_types: Any, workspace_id: str, amount: int, *, shard: int = UNSHARDED
) -> bool:
    """Atomically reserve `amount` against the workspace credit balance.

    True = accepted (row-count 1); False = insufficient credits (row-count 0).
    """
    # Static table name (literal, not interpolated) + bound params only.
    sql = (
        "UPDATE tr_credit_balance SET reserved = reserved + @est "
        "WHERE workspace_id=@ws AND shard=@shard "
        "AND (total_credits - total_usage - reserved) >= @est"
    )
    count = transaction.execute_update(
        sql,
        params={"est": int(amount), "ws": workspace_id, "shard": shard},
        param_types={
            "est": param_types.INT64,
            "ws": param_types.STRING,
            "shard": param_types.INT64,
        },
    )
    return count == 1


def release_credit(
    transaction: Any,
    param_types: Any,
    workspace_id: str,
    hold: int,
    actual: int,
    *,
    shard: int = UNSHARDED,
) -> int:
    """Release the EXACT recorded credit hold and book `actual` usage.

    For refund pass actual=0 (releases the hold, books no usage). Returns the
    modified-row count; the caller asserts == 1 (a 0-row release must not be
    silently accepted — it strands the hold and loses the charge).
    """
    sql = (
        "UPDATE tr_credit_balance "
        "SET reserved = reserved - @hold, total_usage = total_usage + @actual "
        "WHERE workspace_id=@ws AND shard=@shard"
    )
    return transaction.execute_update(
        sql,
        params={"hold": int(hold), "actual": int(actual), "ws": workspace_id, "shard": shard},
        param_types={
            "hold": param_types.INT64,
            "actual": param_types.INT64,
            "ws": param_types.STRING,
            "shard": param_types.INT64,
        },
    )
