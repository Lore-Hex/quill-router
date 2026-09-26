"""Strict window admission: one conditional write, then one locked point read."""

from __future__ import annotations

from typing import Any

from trusted_router.spend_windows import (
    KeyWindowLimitDecision,
    KeyWindowLimitExceeded,
    decide_key_window_limits,
    utcnow,
    window_floors,
)
from trusted_router.storage_gcp_counter_dml import (
    KEY_ACCEPTED,
    KEY_INSUFFICIENT,
    KEY_MISSING,
    KEY_NO_HOLD,
)


def reserve_strict_key(
    transaction: Any, pt: Any, key_hash: str, amount: int, *, is_byok: bool, enforce_windows: bool
) -> tuple[str, KeyWindowLimitDecision | None]:
    now = utcnow()
    floors = window_floors(now)
    # All outstanding holds count even across midnight: they could settle in
    # the new window. Settlement releases the hold and books actual usage in
    # its current UTC windows using the existing exactly-once reservation.
    sql = (
        "UPDATE tr_key_limit SET reserved = reserved + @est "
        "WHERE key_hash=@kh AND shard=0 "
        "AND (@is_byok=FALSE OR include_byok=TRUE) "
        "AND (limit_micro IS NULL OR limit_micro - usage "
        "- IF(include_byok, byok_usage, 0) - reserved >= @est)"
    )
    params: dict[str, Any] = {"kh": key_hash, "est": amount, "is_byok": is_byok}
    types = {"kh": pt.STRING, "est": pt.INT64, "is_byok": pt.BOOL}
    if enforce_windows:
        for window, prefix in (("daily", "day"), ("weekly", "week"), ("monthly", "month")):
            sql += (
                f" AND ({prefix}_limit_micro IS NULL OR {prefix}_limit_micro "
                f"- IF({prefix}_start IS NULL OR {prefix}_start < @{prefix}_floor, 0, {prefix}_usage) "
                "- reserved >= @est)"
            )
            params[f"{prefix}_floor"] = floors[window]
            types[f"{prefix}_floor"] = pt.TIMESTAMP
    count = transaction.execute_update(sql, params=params, param_types=types)
    rows = list(
        transaction.execute_sql(
            "SELECT include_byok, reserved, day_limit_micro, day_usage, day_start, "
            "week_limit_micro, week_usage, week_start, month_limit_micro, month_usage, month_start "
            "FROM tr_key_limit WHERE key_hash=@kh AND shard=0",
            params={"kh": key_hash},
            param_types={"kh": pt.STRING},
        )
    )
    if not rows:
        return KEY_MISSING, None
    row = rows[0]
    if is_byok and not row[0]:
        return KEY_NO_HOLD, None
    held = int(row[1]) - (amount if count == 1 else 0)
    limits, used = {}, {}
    if enforce_windows:
        for offset, window in ((2, "daily"), (5, "weekly"), (8, "monthly")):
            limit, usage, started = row[offset : offset + 3]
            if limit is not None:
                limits[window] = int(limit)
                used[window] = held + (
                    int(usage or 0) if started is not None and started >= floors[window] else 0
                )
    decision = decide_key_window_limits(limits, used, amount, now=now)
    if count == 1:
        return KEY_ACCEPTED, decision
    if decision is not None and not decision.allowed:
        raise KeyWindowLimitExceeded(decision)
    return KEY_INSUFFICIENT, decision
