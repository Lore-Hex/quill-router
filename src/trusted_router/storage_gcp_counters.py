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

CREDIT_BALANCE_COLUMNS = (
    "workspace_id",
    "shard",
    "total_credits",
    "total_usage",
    "reserved",
    "source_updated_at",
    "updated_at",
)

KEY_LIMIT_COLUMNS = (
    "key_hash",
    "shard",
    "limit_micro",
    "usage",
    "byok_usage",
    "reserved",
    "include_byok",
    "source_updated_at",
    "updated_at",
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dataclass (CreditAccount/ApiKey) or a dict."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def credit_balance_mirror_row(workspace_id: str, value: Any, commit_ts: Any) -> tuple:
    """Exact post-write mirror of a `credit` row into tr_credit_balance."""
    return (
        workspace_id,
        UNSHARDED,
        int(_field(value, "total_credits_microdollars", 0)),
        int(_field(value, "total_usage_microdollars", 0)),
        int(_field(value, "reserved_microdollars", 0)),
        commit_ts,  # source_updated_at — the JSON row's updated_at, same commit
        commit_ts,  # this mirror's updated_at
    )


def key_limit_mirror_row(key_hash: str, value: Any, commit_ts: Any) -> tuple:
    """Exact post-write mirror of an `api_key` row into tr_key_limit."""
    limit = _field(value, "limit_microdollars", None)
    return (
        key_hash,
        UNSHARDED,
        None if limit is None else int(limit),
        int(_field(value, "usage_microdollars", 0)),
        int(_field(value, "byok_usage_microdollars", 0)),
        int(_field(value, "reserved_microdollars", 0)),
        bool(_field(value, "include_byok_in_limit", True)),
        commit_ts,
        commit_ts,
    )


def mirror_write(writer: Any, kind: str, entity_id: str, value: Any, commit_ts: Any) -> None:
    """If `kind` is a hot counter, mirror it onto its typed table via `writer`.

    `writer` is a Spanner transaction or batch exposing ``insert_or_update``.
    Called immediately after the authoritative ``tr_entities`` write on the same
    writer, so the mirror commits atomically with it. No-op for other kinds.
    """
    if kind == "credit":
        writer.insert_or_update(
            table=CREDIT_BALANCE_TABLE,
            columns=CREDIT_BALANCE_COLUMNS,
            values=[credit_balance_mirror_row(entity_id, value, commit_ts)],
        )
    elif kind == "api_key":
        writer.insert_or_update(
            table=KEY_LIMIT_TABLE,
            columns=KEY_LIMIT_COLUMNS,
            values=[key_limit_mirror_row(entity_id, value, commit_ts)],
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
    writer.delete(
        table,
        spanner_module.KeySet(keys=[(entity_id, UNSHARDED) for entity_id in entity_ids]),
    )


# ── Step 2: reconciliation (pure drift detection) ───────────────────────────
# The per-write mirror is atomic (same txn) so it cannot tear, but an INDEPENDENT
# full-row comparator is the red-team P2 defense: it catches a missing typed row
# (pre-flag rows not yet backfilled) or any value divergence, which the per-write
# path is structurally blind to. The flip to typed enforcement (Step 3) is gated
# on this comparator reading zero across production.

# JSON body field  ->  typed-table column
CREDIT_DRIFT_FIELDS = {
    "total_credits_microdollars": "total_credits",
    "total_usage_microdollars": "total_usage",
    "reserved_microdollars": "reserved",
}
KEY_DRIFT_FIELDS = {
    "limit_microdollars": "limit_micro",
    "usage_microdollars": "usage",
    "byok_usage_microdollars": "byok_usage",
    "reserved_microdollars": "reserved",
    "include_byok_in_limit": "include_byok",
}


def _drift(json_body: dict, typed_row: dict | None, fields: dict[str, str]) -> dict:
    """Return {field: (json_value, typed_value)} for every mismatch.

    typed_row None (missing mirror) reports every field as drift. A normalized
    int/bool/None compare avoids false positives from JSON 0-vs-missing.
    """
    out: dict[str, tuple] = {}
    for json_field, typed_col in fields.items():
        jv = json_body.get(json_field, 0)
        tv = None if typed_row is None else typed_row.get(typed_col)
        # default-0 for int counters when the JSON omits the field
        if jv is None and typed_col != "limit_micro":
            jv = 0
        if not isinstance(jv, bool) and isinstance(jv, int | type(None)) and tv is not None:
            tv = int(tv) if not isinstance(tv, bool) and tv is not None else tv
        if jv != tv:
            out[typed_col] = (jv, tv)
    return out


def credit_drift(json_body: dict, typed_row: dict | None) -> dict:
    """Mismatched credit counters between the authoritative JSON and typed row."""
    return _drift(json_body, typed_row, CREDIT_DRIFT_FIELDS)


def key_drift(json_body: dict, typed_row: dict | None) -> dict:
    """Mismatched api_key counters between the authoritative JSON and typed row."""
    return _drift(json_body, typed_row, KEY_DRIFT_FIELDS)
