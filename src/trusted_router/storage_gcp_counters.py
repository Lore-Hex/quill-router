"""Typed-counter mirror — Step 1 of the billing typed-column migration.

See docs/design/billing-typed-counters.md.

During Step 1 the JSON ``tr_entities`` rows for kind ``credit`` and ``api_key``
stay **authoritative**. Every write of one of those rows ALSO mirrors the exact
post-write counter values into typed Spanner tables (``tr_credit_balance`` /
``tr_key_limit``) as a **mutation in the SAME transaction/batch** as the JSON
write. Same-transaction mutation + mutation (never DML here) means the typed row
can never *tear* from the JSON row — they commit atomically or not at all.

Deliberately there are **no conditional predicates** in this mirror: a shadow
that ran accept/reject logic would manufacture drift by design (red-team P2).
The mirror writes absolute post-transaction values (idempotent under the JSON
RMW retry), so re-running the transaction yields the same typed row.

Enforcement stays on the JSON path until Step 3 flips it to conditional DML on
these same typed tables.
"""

from __future__ import annotations

from typing import Any

CREDIT_BALANCE_TABLE = "tr_credit_balance"
KEY_LIMIT_TABLE = "tr_key_limit"

# Long tail lives entirely on shard 0; sharding a whale is a data change later.
UNSHARDED = 0
MAX_CREDIT_SHARDS = 64
MAX_KEY_USAGE_SHARDS = 64

# OWNERSHIP SPLIT (2026-06-25 incident). The JSON->typed mirror writes ONLY the
# columns JSON owns. `total_credits` is set by credit events (top-ups / grants /
# refunds-as-credit, all via credit_workspace_once). `reserved` + `total_usage`
# are owned by the typed authorize/finalize DML, so the mirror must NOT write
# them — a full-row mirror clobbers an in-flight typed hold and the next finalize
# fails "release row-count != 1". Columns dropped from the mirror keep their
# NOT NULL DEFAULT(0), so a first-write insert still lands reserved/total_usage=0.
CREDIT_BALANCE_COLUMNS = (
    "workspace_id",
    "shard",
    "total_credits",
    "source_updated_at",
    "updated_at",
)

# Same split for keys: JSON owns config (limit_micro, the per-window
# *_limit_micro caps, include_byok); the typed DML owns usage / byok_usage /
# reserved AND the window usage state (day/week/month usage + starts). Mirror
# config only — mirroring a window counter would re-create the #79 clobber.
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


def credit_balance_mirror_row(workspace_id: str, value: Any, commit_ts: Any) -> tuple:
    """Mirror the JSON-owned `total_credits` of a `credit` row into
    tr_credit_balance. reserved + total_usage are typed-DML-owned and are
    deliberately NOT mirrored (see CREDIT_BALANCE_COLUMNS).

    This generic absolute-value mirror is intentionally one-shard only. Once a
    workspace is explicitly sharded, credit deltas are distributed by
    credit_workspace_typed_direct; replaying the global JSON total into shard 0
    would multiply its budget.
    """
    return (
        workspace_id,
        UNSHARDED,
        int(_field(value, "total_credits_microdollars", 0)),
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
    """Mirror config to every active usage row without touching typed counters."""
    shard_count = key_usage_shard_count(value)
    base = key_limit_mirror_row(key_hash, value, commit_ts)
    return [base[:1] + (shard,) + base[2:] for shard in range(shard_count)]


def mirror_write(writer: Any, kind: str, entity_id: str, value: Any, commit_ts: Any) -> None:
    """If `kind` is a hot counter, mirror it onto its typed table via `writer`.

    `writer` is a Spanner transaction or batch exposing ``insert_or_update``.
    Called immediately after the authoritative ``tr_entities`` write on the same
    writer, so the mirror commits atomically with it. No-op for other kinds.
    """
    if kind == "credit":
        if credit_shard_count(value) != 1:
            return
        writer.insert_or_update(
            table=CREDIT_BALANCE_TABLE,
            columns=CREDIT_BALANCE_COLUMNS,
            values=[credit_balance_mirror_row(entity_id, value, commit_ts)],
        )
    elif kind == "api_key":
        writer.insert_or_update(
            table=KEY_LIMIT_TABLE,
            columns=KEY_LIMIT_COLUMNS,
            values=key_limit_mirror_rows(entity_id, value, commit_ts),
        )


def _typed_table_for(kind: str) -> str | None:
    if kind == "credit":
        return CREDIT_BALANCE_TABLE
    if kind == "api_key":
        return KEY_LIMIT_TABLE
    return None


def mirror_delete(writer: Any, kind: str, entity_ids: list[str], spanner_module: Any) -> None:
    """Mirror a credit/api_key delete onto its typed table on the same writer.

    Without this, deleting the authoritative JSON row leaves a stale typed row
    behind — exact-mirror drift that would poison Step 2 reconciliation. No-op
    for non-counter kinds.
    """
    table = _typed_table_for(kind)
    if table is None:
        return
    max_shards = MAX_CREDIT_SHARDS if kind == "credit" else MAX_KEY_USAGE_SHARDS
    writer.delete(
        table,
        spanner_module.KeySet(
            keys=[
                (entity_id, shard)
                for entity_id in entity_ids
                for shard in range(max_shards)
            ]
        ),
    )


# ── Step 2: reconciliation (pure drift detection) ───────────────────────────
# The per-write mirror is atomic (same txn) so it cannot tear, but an INDEPENDENT
# full-row comparator is the red-team P2 defense: it catches a missing typed row
# (pre-flag rows not yet backfilled) or any value divergence, which the per-write
# path is structurally blind to. The flip to typed enforcement (Step 3) is gated
# on this comparator reading zero across production.

# (json body field, typed column, default-when-the-json-field-is-absent).
# Defaults MUST match the model + the mirror writer so legacy JSON rows that
# omit a field don't read as false drift.
# OWNERSHIP SPLIT (2026-06-25): only the JSON-owned columns are comparable. The
# typed DML owns reserved/total_usage (credit) and usage/byok_usage/reserved
# (key); JSON is intentionally stale for those after typed enforcement, so they
# are NOT drift-checked (and the mirror/backfill no longer write them).
CREDIT_DRIFT_FIELDS = (
    ("total_credits_microdollars", "total_credits", 0),
)
KEY_DRIFT_FIELDS = (
    ("limit_microdollars", "limit_micro", None),
    ("limit_daily_microdollars", "day_limit_micro", None),
    ("limit_weekly_microdollars", "week_limit_micro", None),
    ("limit_monthly_microdollars", "month_limit_micro", None),
    ("include_byok_in_limit", "include_byok", True),
)


def _norm(value: Any, default: Any) -> Any:
    """Normalize a value to its default's type for an apples-to-apples compare.

    bool default -> bool; int default -> int; None-default (nullable limit) keeps
    None or coerces to int. A None json value falls back to the field default.
    """
    if isinstance(default, bool):
        return default if value is None else bool(value)
    if value is None:
        return None if default is None else int(default)
    return int(value)


def _drift(json_body: dict, typed_row: dict | None, fields: tuple) -> dict:
    """Return {typed_col: (json_value, typed_value)} for every mismatch.

    typed_row None (missing mirror) reports drift on every field whose default is
    not None (so a missing mirror is always flagged via at least one field).
    """
    out: dict[str, tuple] = {}
    for json_field, typed_col, default in fields:
        jv = _norm(json_body.get(json_field, default), default)
        tv = None if typed_row is None else _norm(typed_row.get(typed_col), default)
        if jv != tv:
            out[typed_col] = (jv, tv)
    return out


def credit_drift(json_body: dict, typed_row: dict | None) -> dict:
    """Mismatched credit counters between the authoritative JSON and typed row."""
    return _drift(json_body, typed_row, CREDIT_DRIFT_FIELDS)


def key_drift(json_body: dict, typed_row: dict | None) -> dict:
    """Mismatched api_key counters between the authoritative JSON and typed row."""
    return _drift(json_body, typed_row, KEY_DRIFT_FIELDS)
