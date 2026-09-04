"""Typed-counter table constants and creation-time row builders.

The retired JSON-to-typed mirror used these builders for every credit/api_key
entity write. C2a keeps creation seeding explicit; API-key limit edits also use
the config-only builder with strongly-read counters so escrowed lifetime limits
can be repartitioned without touching money/usage state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

CREDIT_BALANCE_TABLE = "tr_credit_balance"
KEY_LIMIT_TABLE = "tr_key_limit"

# Long tail lives entirely on shard 0; sharding a whale is a data change later.
UNSHARDED = 0
MAX_CREDIT_SHARDS = 64
MAX_KEY_USAGE_SHARDS = 64
DEFAULT_NEW_BILLING_SHARDS = 16

CREDIT_BALANCE_TRUST_COLUMNS = (
    "trust_tier",
    "trust_computed_at",
    "trust_latched_at",
    "trust_override_tier",
    "billing_pause_causes",
    "pause_epoch",
    "trust_reconciled_through",
)

# Creation-time credit seed columns. `reserved` + `total_usage` are deliberately
# omitted so a new row gets the Spanner defaults (0) and later typed DML owns
# those counters exclusively.
CREDIT_BALANCE_COLUMNS = (
    "workspace_id",
    "shard",
    "total_credits",
    *CREDIT_BALANCE_TRUST_COLUMNS,
    "source_updated_at",
    "updated_at",
)

# Creation-time key seed columns. Usage/reserved/window usage are omitted so a
# new row starts at defaults and subsequent typed DML owns them.
KEY_LIMIT_COLUMNS = (
    "key_hash",
    "shard",
    "limit_micro",
    "day_limit_micro",
    "week_limit_micro",
    "month_limit_micro",
    "include_byok",
    "source_updated_at",
    "updated_at",
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dataclass (CreditAccount/ApiKey) or a dict."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def credit_shard_count(value: Any) -> int:
    """Return the configured credit-ledger shard count.

    Legacy CreditAccount JSON omits this field and therefore remains exactly
    one-shard. Invalid persisted values fail closed instead of silently
    changing which sub-ledgers enforce the workspace cap.
    """
    raw = _field(value, "shard_count", 1)
    if isinstance(raw, bool):
        raise ValueError("credit shard_count must be a positive integer")
    count = int(raw)
    if count < 1:
        raise ValueError("credit shard_count must be a positive integer")
    if count > MAX_CREDIT_SHARDS:
        raise ValueError(f"credit shard_count must not exceed {MAX_CREDIT_SHARDS}")
    return count


def key_usage_shard_count(value: Any) -> int:
    """Return and validate an API key's usage-counter shard count.

    Exact lifetime limits use escrowed per-shard sub-budgets whose sum is the
    configured limit. Daily/weekly/monthly limits remain approximate snapshot
    checks whose usage is summed over every shard.
    """
    raw = _field(value, "usage_shard_count", 1)
    if isinstance(raw, bool):
        raise ValueError("key usage_shard_count must be a positive integer")
    count = int(raw)
    if count < 1:
        raise ValueError("key usage_shard_count must be a positive integer")
    if count > MAX_KEY_USAGE_SHARDS:
        raise ValueError(
            f"key usage_shard_count must not exceed {MAX_KEY_USAGE_SHARDS}"
        )
    return count


def distribute_credit_amount(amount: int, shard_count: int) -> tuple[int, ...]:
    """Evenly partition a grant delta, putting the remainder on shard zero."""
    if shard_count < 1:
        raise ValueError("credit shard_count must be a positive integer")
    sign = -1 if amount < 0 else 1
    per_shard, remainder = divmod(abs(int(amount)), shard_count)
    values = [sign * per_shard for _ in range(shard_count)]
    values[UNSHARDED] += sign * remainder
    return tuple(values)


def partition_key_limit(
    limit_micro: int | None,
    *,
    usage_parts: Sequence[int],
    byok_usage_parts: Sequence[int],
    reserved_parts: Sequence[int],
    include_byok: bool,
) -> tuple[int | None, ...]:
    """Partition an exact key cap into independent escrow sub-budgets.

    Every shard receives its already-consumed amount plus a share of remaining
    headroom. Therefore the row limits always sum to ``limit_micro`` and no
    distribution of future reservations can exceed the global cap. When a cap
    is lowered below current consumption, every shard is left with non-positive
    headroom so new reservations fail closed while existing holds can settle.
    """
    shard_count = len(usage_parts)
    if shard_count < 1:
        raise ValueError("key limit partition requires at least one shard")
    if len(byok_usage_parts) != shard_count or len(reserved_parts) != shard_count:
        raise ValueError("key limit partition inputs must have equal lengths")
    if limit_micro is None:
        return (None,) * shard_count
    limit = int(limit_micro)
    if limit < 0:
        raise ValueError("key lifetime limit must not be negative")

    consumed: list[int] = []
    for usage, byok_usage, reserved in zip(
        usage_parts,
        byok_usage_parts,
        reserved_parts,
        strict=True,
    ):
        values = (int(usage), int(byok_usage), int(reserved))
        if any(value < 0 for value in values):
            raise ValueError("key limit partition counters must not be negative")
        consumed.append(values[0] + (values[1] if include_byok else 0) + values[2])

    remaining = limit - sum(consumed)
    if remaining >= 0:
        headroom = distribute_credit_amount(remaining, shard_count)
        return tuple(
            current + extra
            for current, extra in zip(consumed, headroom, strict=True)
        )

    # The cap was reduced below spend already booked or held. Subtract the
    # deficit from consumed allocations without creating headroom on any row.
    # Settlement does not require limit >= consumption, so existing holds can
    # still close while every new reserve is rejected.
    allocations = list(consumed)
    deficit = -remaining
    for shard in sorted(
        range(shard_count),
        key=lambda index: (allocations[index], -index),
        reverse=True,
    ):
        reduction = min(allocations[shard], deficit)
        allocations[shard] -= reduction
        deficit -= reduction
        if deficit == 0:
            break
    if deficit != 0:  # pragma: no cover - limit>=0 makes this unreachable
        raise AssertionError("key limit partition deficit was not exhausted")
    return tuple(allocations)


def credit_balance_mirror_row(workspace_id: str, total_micro: int, commit_ts: Any) -> tuple:
    """Seed the one-shard `total_credits` value into tr_credit_balance.

    reserved + total_usage are typed-DML-owned and are deliberately NOT mirrored
    (see CREDIT_BALANCE_COLUMNS).

    This absolute-value seed is intentionally one-shard only. Once a
    workspace is explicitly sharded, credit deltas are distributed by
    credit_workspace_typed_direct; replaying the global JSON total into shard 0
    would multiply its budget.
    """
    return (
        workspace_id,
        UNSHARDED,
        int(total_micro),
        0,
        None,
        None,
        None,
        [],
        0,
        None,
        commit_ts,  # source_updated_at — the JSON row's updated_at, same commit
        commit_ts,  # this mirror's updated_at
    )


def credit_balance_seed_rows(
    workspace_id: str,
    total_micro: int,
    commit_ts: Any,
    *,
    shard_count: int,
) -> list[tuple]:
    """Seed a new workspace's exact balance across independent sub-ledgers."""

    count = credit_shard_count({"shard_count": shard_count})
    parts = distribute_credit_amount(total_micro, count)
    return [
        (
            workspace_id,
            shard,
            parts[shard],
            0,
            None,
            None,
            None,
            [],
            0,
            None,
            commit_ts,
            commit_ts,
        )
        for shard in range(count)
    ]


def key_limit_mirror_row(key_hash: str, value: Any, commit_ts: Any) -> tuple:
    """Mirror the JSON-owned config (limit_micro, window *_limit_micro,
    include_byok) of an `api_key` row into tr_key_limit. usage / byok_usage /
    reserved and the window usage state are typed-DML-owned and are
    deliberately NOT mirrored (see KEY_LIMIT_COLUMNS)."""
    limit = _field(value, "limit_microdollars", None)
    day = _field(value, "limit_daily_microdollars", None)
    week = _field(value, "limit_weekly_microdollars", None)
    month = _field(value, "limit_monthly_microdollars", None)
    return (
        key_hash,
        UNSHARDED,
        None if limit is None else int(limit),
        None if day is None else int(day),
        None if week is None else int(week),
        None if month is None else int(month),
        bool(_field(value, "include_byok_in_limit", True)),
        commit_ts,
        commit_ts,
    )


def key_limit_mirror_rows(
    key_hash: str,
    value: Any,
    commit_ts: Any,
    *,
    usage_rows: Sequence[Sequence[int]] | None = None,
) -> list[tuple]:
    """Build config rows for every active usage shard.

    Creation supplies no usage rows and starts every counter at zero. A config
    update supplies the strongly-read ``(shard, usage, byok_usage, reserved)``
    rows so an exact cap can be repartitioned without minting headroom.
    """
    shard_count = key_usage_shard_count(value)
    base = key_limit_mirror_row(key_hash, value, commit_ts)
    if usage_rows is None:
        usage_parts = [0] * shard_count
        byok_usage_parts = [0] * shard_count
        reserved_parts = [0] * shard_count
    else:
        ordered = sorted(usage_rows, key=lambda row: int(row[0]))
        if [int(row[0]) for row in ordered] != list(range(shard_count)):
            raise RuntimeError("configured tr_key_limit usage shard set is incomplete")
        usage_parts = [int(row[1]) for row in ordered]
        byok_usage_parts = [int(row[2]) for row in ordered]
        reserved_parts = [int(row[3]) for row in ordered]
    limits = partition_key_limit(
        _field(value, "limit_microdollars", None),
        usage_parts=usage_parts,
        byok_usage_parts=byok_usage_parts,
        reserved_parts=reserved_parts,
        include_byok=bool(_field(value, "include_byok_in_limit", True)),
    )
    return [
        base[:1] + (shard, limits[shard]) + base[3:]
        for shard in range(shard_count)
    ]
