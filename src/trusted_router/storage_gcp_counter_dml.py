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

# reserve_key outcomes (the per-key spend-cap counterpart of reserve_credit).
KEY_ACCEPTED = "accepted"  # hold taken (row-count 1)
KEY_NO_HOLD = "no_hold"  # uncapped key, or BYOK excluded from the cap: proceed
KEY_INSUFFICIENT = "insufficient"  # capped and over the cap -> 402
KEY_MISSING = "missing"  # typed row absent -> fail closed (drift / not backfilled)


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

    The `reserved >= @hold` guard makes a stale/double release a 0-row no-op
    instead of driving `reserved` negative (which would inflate apparent
    availability) — row-count 0 trips the caller's assert/alarm.
    """
    sql = (
        "UPDATE tr_credit_balance "
        "SET reserved = reserved - @hold, total_usage = total_usage + @actual "
        "WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold"
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


def reserve_key(
    transaction: Any,
    param_types: Any,
    key_hash: str,
    amount: int,
    *,
    is_byok: bool,
    shard: int = UNSHARDED,
) -> str:
    """Atomically reserve `amount` against the per-key spend cap.

    Single conditional UPDATE: it matches (and holds) only a capped key whose
    available headroom covers `amount`, AND only when the cap applies to this
    usage type (BYOK is skipped when the key excludes BYOK). On row-count 0 we
    classify with a point-read IN THE SAME TRANSACTION (codex#2 #3) so the 0 is
    not ambiguous: missing row vs uncapped vs BYOK-excluded vs truly insufficient.

    Returns one of KEY_ACCEPTED / KEY_NO_HOLD / KEY_INSUFFICIENT / KEY_MISSING.
    """
    sql = (
        "UPDATE tr_key_limit SET reserved = reserved + @est "
        "WHERE key_hash=@kh AND shard=@shard AND limit_micro IS NOT NULL "
        "AND (@is_byok = FALSE OR include_byok = TRUE) "
        "AND (limit_micro - usage - IF(include_byok, byok_usage, 0) - reserved) >= @est"
    )
    count = transaction.execute_update(
        sql,
        params={"est": int(amount), "kh": key_hash, "shard": shard, "is_byok": bool(is_byok)},
        param_types={
            "est": param_types.INT64,
            "kh": param_types.STRING,
            "shard": param_types.INT64,
            "is_byok": param_types.BOOL,
        },
    )
    if count == 1:
        return KEY_ACCEPTED
    rows = list(
        transaction.execute_sql(
            "SELECT limit_micro, include_byok FROM tr_key_limit "
            "WHERE key_hash=@kh AND shard=@shard",
            params={"kh": key_hash, "shard": shard},
            param_types={"kh": param_types.STRING, "shard": param_types.INT64},
        )
    )
    if not rows:
        return KEY_MISSING
    limit_micro, include_byok = rows[0][0], rows[0][1]
    if limit_micro is None:
        return KEY_NO_HOLD  # uncapped
    if is_byok and not include_byok:
        return KEY_NO_HOLD  # BYOK excluded from this key's cap
    return KEY_INSUFFICIENT  # capped and over the cap


def release_key(
    transaction: Any,
    param_types: Any,
    key_hash: str,
    hold: int,
    actual: int,
    *,
    book_to_byok: bool,
    shard: int = UNSHARDED,
) -> int:
    """Release the EXACT recorded key hold and book `actual` to usage/byok_usage.

    `hold` is the exact amount taken at reserve (0 if no hold was taken — uncapped
    or BYOK-excluded); `book_to_byok` selects the usage column by the SETTLED
    usage type. Refund = actual 0. The `reserved >= @hold` guard makes a
    stale/double release a 0-row no-op rather than driving reserved negative.
    Returns the modified-row count (caller asserts == 1).
    """
    if book_to_byok:
        sql = (
            "UPDATE tr_key_limit "
            "SET reserved = reserved - @hold, byok_usage = byok_usage + @actual "
            "WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold"
        )
    else:
        sql = (
            "UPDATE tr_key_limit "
            "SET reserved = reserved - @hold, usage = usage + @actual "
            "WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold"
        )
    return transaction.execute_update(
        sql,
        params={"hold": int(hold), "actual": int(actual), "kh": key_hash, "shard": shard},
        param_types={
            "hold": param_types.INT64,
            "actual": param_types.INT64,
            "kh": param_types.STRING,
            "shard": param_types.INT64,
        },
    )
