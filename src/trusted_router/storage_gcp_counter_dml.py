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


# Lazy per-window bump, appended to the release UPDATE. `@wamt` is the amount
# that counts toward the key's caps for this settle (0 for a BYOK settle on a
# key that excludes BYOK — computed in SQL off the row's own include_byok so it
# matches reserve semantics). A stale window (start < current floor) is
# replaced, not added to — that IS the reset; no cron ever runs. COALESCE:
# rows that predate the window DDL read NULL usage until first touched
# (Spanner can't ADD COLUMN NOT NULL on an existing table).
_WINDOW_BUMP_SQL = (
    ", day_usage = IF(day_start IS NULL OR day_start < @day_floor,"
    " @wamt, COALESCE(day_usage, 0) + @wamt)"
    ", day_start = IF(day_start IS NULL OR day_start < @day_floor, @day_floor, day_start)"
    ", week_usage = IF(week_start IS NULL OR week_start < @week_floor,"
    " @wamt, COALESCE(week_usage, 0) + @wamt)"
    ", week_start = IF(week_start IS NULL OR week_start < @week_floor, @week_floor, week_start)"
    ", month_usage = IF(month_start IS NULL OR month_start < @month_floor,"
    " @wamt, COALESCE(month_usage, 0) + @wamt)"
    ", month_start = IF(month_start IS NULL OR month_start < @month_floor, @month_floor, month_start)"
)


def release_key(
    transaction: Any,
    param_types: Any,
    key_hash: str,
    hold: int,
    actual: int,
    *,
    book_to_byok: bool,
    window_floors: dict[str, Any],
    shard: int = UNSHARDED,
) -> int:
    """Release the EXACT recorded key hold and book `actual` to usage/byok_usage,
    and bump the lazy per-window counters in the same statement.

    `hold` is the exact amount taken at reserve (0 if no hold was taken — uncapped
    or BYOK-excluded); `book_to_byok` selects the usage column by the SETTLED
    usage type. Refund = actual 0 (window bump is then +0 — a no-op that still
    lazily rolls the window forward, which is harmless). `window_floors` is
    spend_windows.window_floors(now). The `reserved >= @hold` guard makes a
    stale/double release a 0-row no-op rather than driving reserved negative.
    Returns the modified-row count (caller asserts == 1).
    """
    usage_col = "byok_usage" if book_to_byok else "usage"
    # BYOK settles count toward the caps (incl. windows) only when the key's own
    # include_byok says so — gated in SQL so it matches reserve semantics. On an
    # excluded settle the bump is +0, but a stale window still rolls forward.
    wamt = "IF(include_byok, @actual, 0)" if book_to_byok else "@actual"
    # usage_col/wamt are compile-time constants picked by a bool; values bind as params.
    sql = (
        "UPDATE tr_key_limit "  # noqa: S608
        f"SET reserved = reserved - @hold, {usage_col} = {usage_col} + @actual"
        + _WINDOW_BUMP_SQL.replace("@wamt", wamt)
        + " WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold"
    )
    return transaction.execute_update(
        sql,
        params={
            "hold": int(hold),
            "actual": int(actual),
            "kh": key_hash,
            "shard": shard,
            "day_floor": window_floors["daily"],
            "week_floor": window_floors["weekly"],
            "month_floor": window_floors["monthly"],
        },
        param_types={
            "hold": param_types.INT64,
            "actual": param_types.INT64,
            "kh": param_types.STRING,
            "shard": param_types.INT64,
            "day_floor": param_types.TIMESTAMP,
            "week_floor": param_types.TIMESTAMP,
            "month_floor": param_types.TIMESTAMP,
        },
    )


def key_limit_exists(
    transaction: Any,
    param_types: Any,
    key_hash: str,
    *,
    shard: int = UNSHARDED,
) -> bool:
    """Classify a 0-row key release inside the same read-write transaction."""
    rows = list(
        transaction.execute_sql(
            "SELECT 1 FROM tr_key_limit WHERE key_hash=@kh AND shard=@shard",
            params={"kh": key_hash, "shard": shard},
            param_types={"kh": param_types.STRING, "shard": param_types.INT64},
        )
    )
    return bool(rows)


# ── tr_reservation: durable hold record + scoped idempotency + settle claim ──
# The reservation row records the EXACT holds taken at authorize (so settle
# releases exactly those, codex#1 #5), the resolved usage types (hold vs settled,
# codex#2 #2), and the scoped idempotency key. The atomic authorize INSERTs it;
# settle/refund CLAIM it (first-writer-wins). All DML so it composes into the
# DML-only authorize/settle transactions (no mutation mixing).

RESERVATION_COLUMNS = (
    "reservation_id", "workspace_id", "key_hash", "ws_shard", "key_shard",
    "credit_reserved_micro", "key_reserved_micro", "hold_usage_type",
    "authorization_id", "idempotency_scope", "idempotency_fingerprint", "expires_at",
)


def read_reservation_by_idempotency(
    transaction: Any, param_types: Any, idempotency_scope: str
) -> dict | None:
    """Point-read the existing reservation for a scoped idempotency key (replay).

    Returns the row (incl. fingerprint + the exact holds) or None. The caller
    verifies the fingerprint and replays without re-debiting.
    """
    rows = list(
        transaction.execute_sql(
            "SELECT reservation_id, credit_reserved_micro, key_reserved_micro, "
            "hold_usage_type, authorization_id, idempotency_fingerprint, settled "
            "FROM tr_reservation WHERE idempotency_scope=@scope",
            params={"scope": idempotency_scope},
            param_types={"scope": param_types.STRING},
        )
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "reservation_id": r[0],
        "credit_reserved_micro": r[1],
        "key_reserved_micro": r[2],
        "hold_usage_type": r[3],
        "authorization_id": r[4],
        "idempotency_fingerprint": r[5],
        "settled": r[6],
    }


def read_reservation(transaction: Any, param_types: Any, reservation_id: str) -> dict | None:
    """Point-read a reservation by id (for settle/refund and the reaper)."""
    rows = list(
        transaction.execute_sql(
            "SELECT reservation_id, workspace_id, key_hash, ws_shard, key_shard, "
            "credit_reserved_micro, key_reserved_micro, hold_usage_type, "
            "settled_usage_type, actual_micro, authorization_id, settled "
            "FROM tr_reservation WHERE reservation_id=@rid",
            params={"rid": reservation_id},
            param_types={"rid": param_types.STRING},
        )
    )
    if not rows:
        return None
    r = rows[0]
    keys = (
        "reservation_id", "workspace_id", "key_hash", "ws_shard", "key_shard",
        "credit_reserved_micro", "key_reserved_micro", "hold_usage_type",
        "settled_usage_type", "actual_micro", "authorization_id", "settled",
    )
    return dict(zip(keys, r, strict=True))


def insert_reservation(transaction: Any, param_types: Any, **fields: Any) -> None:
    """INSERT a reservation row. Raises ALREADY_EXISTS (NOT retried) on a scoped
    idempotency-key conflict — the caller converts that to the replay path."""
    pt = param_types
    types = {
        "reservation_id": pt.STRING, "workspace_id": pt.STRING, "key_hash": pt.STRING,
        "ws_shard": pt.INT64, "key_shard": pt.INT64,
        "credit_reserved_micro": pt.INT64, "key_reserved_micro": pt.INT64,
        "hold_usage_type": pt.STRING, "authorization_id": pt.STRING,
        "idempotency_scope": pt.STRING, "idempotency_fingerprint": pt.STRING,
        "expires_at": pt.TIMESTAMP,
    }
    cols = ", ".join(RESERVATION_COLUMNS)
    binds = ", ".join(f"@{c}" for c in RESERVATION_COLUMNS)
    transaction.execute_update(
        f"INSERT INTO tr_reservation ({cols}) VALUES ({binds})",  # noqa: S608 - fixed column list
        params={c: fields.get(c) for c in RESERVATION_COLUMNS},
        param_types={c: types[c] for c in RESERVATION_COLUMNS},
    )


def claim_reservation(
    transaction: Any,
    param_types: Any,
    reservation_id: str,
    *,
    actual_micro: int,
    settled_usage_type: str,
) -> bool:
    """Claim a reservation for settle/refund: first caller wins.

    True = this caller won the claim (row-count 1, settled flipped false->true);
    False = already settled (row-count 0, a replay) -> do NOT touch counters.
    Persists `actual_micro` + `settled_usage_type` so the durable reservation
    records the exact settled amount for audit / reaper reconciliation.
    """
    count = transaction.execute_update(
        "UPDATE tr_reservation SET settled=true, actual_micro=@actual, "
        "settled_usage_type=@sut WHERE reservation_id=@rid AND settled=false",
        params={"rid": reservation_id, "actual": int(actual_micro), "sut": settled_usage_type},
        param_types={
            "rid": param_types.STRING,
            "actual": param_types.INT64,
            "sut": param_types.STRING,
        },
    )
    return count == 1


def insert_entity_dml(
    transaction: Any, param_types: Any, kind: str, entity_id: str, body_json: str
) -> None:
    """DML INSERT of a tr_entities JSON row (e.g. gateway_authorization), so it
    composes into the DML-only authorize transaction instead of a mutation.
    PENDING_COMMIT_TIMESTAMP() is the last touch of the row; raises ALREADY_EXISTS
    on a duplicate (kind,id)."""
    transaction.execute_update(
        "INSERT INTO tr_entities (kind, id, body, updated_at) "
        "VALUES (@kind, @id, @body, PENDING_COMMIT_TIMESTAMP())",
        params={"kind": kind, "id": entity_id, "body": body_json},
        param_types={
            "kind": param_types.STRING,
            "id": param_types.STRING,
            "body": param_types.STRING,
        },
    )


def insert_entity_dml_at(
    transaction: Any, param_types: Any, kind: str, entity_id: str, body_json: str, now: Any
) -> None:
    """DML INSERT a tr_entities row with a client `updated_at` (NOT
    PENDING_COMMIT_TIMESTAMP), so MULTIPLE tr_entities DML statements can run in
    one transaction without the PCT same-table trap (codex 3e). Raises
    ALREADY_EXISTS on duplicate (kind,id)."""
    transaction.execute_update(
        "INSERT INTO tr_entities (kind, id, body, updated_at) "
        "VALUES (@kind, @id, @body, @now)",
        params={"kind": kind, "id": entity_id, "body": body_json, "now": now},
        param_types={
            "kind": param_types.STRING, "id": param_types.STRING,
            "body": param_types.STRING, "now": param_types.TIMESTAMP,
        },
    )


def update_entity_body_dml(
    transaction: Any, param_types: Any, kind: str, entity_id: str, body_json: str, now: Any
) -> int:
    """DML UPDATE a tr_entities row's body (e.g. mark gateway_authorization
    settled) with a client `updated_at`. Returns the modified-row count."""
    return transaction.execute_update(
        "UPDATE tr_entities SET body=@body, updated_at=@now "
        "WHERE kind=@kind AND id=@id",
        params={"kind": kind, "id": entity_id, "body": body_json, "now": now},
        param_types={
            "kind": param_types.STRING, "id": param_types.STRING,
            "body": param_types.STRING, "now": param_types.TIMESTAMP,
        },
    )
