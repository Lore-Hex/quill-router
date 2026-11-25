"""The one writer of tr_key_limit's mirrored limits on Spanner.

Every write of the limit columns a strict key's admission reads
(limit_micro, day_limit_micro, week_limit_micro, month_limit_micro) goes
through write_key_limit_rows: key creation, a limit edit, a reshard, an escrow
rebalance. In a read-write transaction it reads the key's current rows and
its entity first, and refuses (StrictKeyFirstLimit) a write that would take a
strict key's mirror from every limit unset to any set, or put a limit on rows
whose key is gone. A batch cannot read, so through one it only inserts a new
key's rows, shard 0 among them: an insert never overwrites a row, and one of
shard 0 fails whole (ALREADY_EXISTS) for a key that has rows.

Python's strict reserve (storage_gcp_strict_budget.reserve_strict_key) and the
Go edge (store.StrictKeyLimit) read these columns, not the entity, so this is
the property the edge's limitless strict keys rest on, whatever writes the
entity (docs/design/fast-admission-e2-strict-keys.md section 3).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE, MAX_KEY_USAGE_SHARDS
from trusted_router.storage_key_patch import StrictKeyFirstLimit

MIRROR_LIMIT_COLUMNS = ("limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro")

_CURRENT_LIMITS_SQL = (
    "SELECT shard, limit_micro, day_limit_micro, week_limit_micro, month_limit_micro "
    "FROM tr_key_limit WHERE key_hash=@kh AND shard>=0 AND shard<@shard_count ORDER BY shard"
)
_KEY_BODY_SQL = "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id"


def write_key_limit_rows(
    writer: Any,
    param_types: Any,
    *,
    key_hash: str,
    columns: Sequence[str],
    values: Sequence[Sequence[Any]],
    mutation: str,
) -> None:
    """Write one key's tr_key_limit rows, refusing a strict key's first limit.

    ``mutation`` is ``insert`` (new rows only), ``insert_or_update`` or
    ``update``. In a transaction every one that sets a limit is checked
    against the key's current rows first; a batch only inserts a new key's
    rows, shard 0 among them.
    """
    columns = tuple(columns)
    rows = [tuple(row) for row in values]
    if columns[:2] != ("key_hash", "shard") or any(len(row) != len(columns) for row in rows):
        raise ValueError("tr_key_limit rows lead with key_hash and shard")
    if any(row[0] != key_hash for row in rows):
        raise ValueError("write_key_limit_rows writes one key's rows")
    if mutation not in ("insert", "insert_or_update", "update"):
        raise ValueError(f"unknown tr_key_limit mutation: {mutation!r}")
    if not hasattr(writer, "execute_sql"):
        if mutation != "insert" or not any(row[1] == 0 for row in rows):
            raise ValueError(
                "a batch cannot read the key's rows, so it only inserts a new key's rows, shard 0 among them"
            )
    elif _sets_a_limit(columns, rows) and _limits_may_not_appear(writer, param_types, key_hash):
        raise StrictKeyFirstLimit()
    # By name: the request path counts dynamic attribute reads.
    if mutation == "insert":
        writer.insert(table=KEY_LIMIT_TABLE, columns=columns, values=rows)
    elif mutation == "update":
        writer.update(table=KEY_LIMIT_TABLE, columns=columns, values=rows)
    else:
        writer.insert_or_update(table=KEY_LIMIT_TABLE, columns=columns, values=rows)


def _sets_a_limit(columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> bool:
    positions = [i for i, column in enumerate(columns) if column in MIRROR_LIMIT_COLUMNS]
    return any(row[i] is not None for row in rows for i in positions)


def _limits_may_not_appear(transaction: Any, param_types: Any, key_hash: str) -> bool:
    """Whether the key's rows hold no limit and must keep holding none: the
    key is strict, or gone (rows a deleted key left behind), or its body does
    not read. A key with no rows yet, or one with a limit, may be written."""
    current = list(transaction.execute_sql(
        _CURRENT_LIMITS_SQL,
        params={"kh": key_hash, "shard_count": MAX_KEY_USAGE_SHARDS + 1},
        param_types={"kh": param_types.STRING, "shard_count": param_types.INT64},
    ))
    if not current or any(value is not None for row in current for value in row[1:]):
        return False
    body = list(transaction.execute_sql(
        _KEY_BODY_SQL,
        params={"kind": "api_key", "id": key_hash},
        param_types={"kind": param_types.STRING, "id": param_types.STRING},
    ))
    if not body:
        return True
    try:
        record = json.loads(body[0][0])
    except (TypeError, ValueError):
        return True
    # A body from before budget_strict existed is not strict (the field's
    # default), as storage_models reads it.
    return not isinstance(record, dict) or record.get("budget_strict") is True
