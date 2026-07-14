"""Typed-counter table constants and creation-time row builders.

The retired JSON-to-typed mirror used these builders for every credit/api_key
entity write. C2a keeps the builders but makes seeding explicit: callers may use
them only while creating a new credit/api_key entity, never while updating
metadata on an existing entity. After creation, the typed conditional-DML paths
own money/usage state.
"""

from __future__ import annotations

from typing import Any

CREDIT_BALANCE_TABLE = "tr_credit_balance"
KEY_LIMIT_TABLE = "tr_key_limit"

# Long tail lives entirely on shard 0; sharding a whale is a data change later.
UNSHARDED = 0
MAX_CREDIT_SHARDS = 64
MAX_KEY_USAGE_SHARDS = 64

# Creation-time credit seed columns. `reserved` + `total_usage` are deliberately
# omitted so a new row gets the Spanner defaults (0) and later typed DML owns
# those counters exclusively.
CREDIT_BALANCE_COLUMNS = (
    "workspace_id",
    "shard",
    "total_credits",
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

    Only fully uncapped keys may fan usage writes across rows. Partitioning
    spend limits needs separate sub-budget/rebalance semantics; accepting such
    a mixed configuration here could weaken a hard user budget, so it fails
    closed instead.
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
    if count > 1 and any(
        _field(value, field_name, None) is not None
        for field_name in (
            "limit_microdollars",
            "limit_daily_microdollars",
            "limit_weekly_microdollars",
            "limit_monthly_microdollars",
        )
    ):
        raise ValueError("only uncapped API keys may use sharded usage counters")
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
        commit_ts,  # source_updated_at — the JSON row's updated_at, same commit
        commit_ts,  # this mirror's updated_at
    )


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


def key_limit_mirror_rows(key_hash: str, value: Any, commit_ts: Any) -> list[tuple]:
    """Build initial config rows for every active usage shard."""
    shard_count = key_usage_shard_count(value)
    base = key_limit_mirror_row(key_hash, value, commit_ts)
    return [base[:1] + (shard,) + base[2:] for shard in range(shard_count)]
