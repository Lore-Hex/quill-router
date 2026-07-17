"""One-time removal of retired money fields from JSON credit metadata.

The typed ``tr_credit_balance`` table is authoritative. This module exists only
for the reviewed operator migration that removes stale pre-C2b counters from
``tr_entities(kind='credit')`` without disturbing Stripe or auto-refill metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from trusted_router.storage_gcp_counters import credit_shard_count

LEGACY_CREDIT_MONEY_FIELDS = frozenset(
    {
        "total_credits_microdollars",
        "total_usage_microdollars",
        "reserved_microdollars",
    }
)


@dataclass(frozen=True)
class CreditJsonCleanupResult:
    workspace_id: str
    legacy_fields: tuple[str, ...] = ()
    expected_shards: tuple[int, ...] = ()
    observed_shards: tuple[int, ...] = ()
    ready: bool = False
    applied: bool = False
    reason: str | None = None

    @property
    def needs_cleanup(self) -> bool:
        return bool(self.legacy_fields)


def inspect_credit_json(store: Any, workspace_id: str) -> CreditJsonCleanupResult:
    """Inspect one metadata row and verify its authoritative typed shard set."""
    with store._database.snapshot(multi_use=True) as snapshot:
        return _inspect_reader(store, snapshot, workspace_id)


def cleanup_credit_json(
    store: Any,
    workspace_id: str,
    *,
    apply: bool = False,
) -> CreditJsonCleanupResult:
    """Remove retired money keys after validating the typed balance in one txn.

    Dry-run is the default. Apply is idempotent and preserves every non-money
    field, including fields unknown to this release.
    """
    if not apply:
        return inspect_credit_json(store, workspace_id)

    def txn(transaction: Any) -> CreditJsonCleanupResult:
        result = _inspect_reader(store, transaction, workspace_id)
        if not result.ready or not result.needs_cleanup:
            return result

        raw = store._read_entity_tx(transaction, "credit", workspace_id, dict)
        if raw is None:
            return replace(result, ready=False, reason="credit metadata row disappeared")
        cleaned = {
            key: value for key, value in raw.items() if key not in LEGACY_CREDIT_MONEY_FIELDS
        }
        store._write_entity_tx(transaction, "credit", workspace_id, cleaned)
        return replace(result, applied=True)

    return store._run_in_transaction(txn)


def legacy_credit_workspace_ids(store: Any) -> list[str]:
    """Return every credit metadata row that still contains retired money keys."""
    with store._database.snapshot() as snapshot:
        rows = snapshot.execute_sql(
            "SELECT id, body FROM tr_entities WHERE kind=@kind ORDER BY id",
            params={"kind": "credit"},
            param_types={"kind": store._param_types.STRING},
        )
        return [
            str(row[0])
            for row in rows
            if LEGACY_CREDIT_MONEY_FIELDS.intersection(json.loads(row[1]))
        ]


def _inspect_reader(store: Any, reader: Any, workspace_id: str) -> CreditJsonCleanupResult:
    raw = store._read_entity_from(reader, "credit", workspace_id, dict)
    if raw is None:
        return CreditJsonCleanupResult(
            workspace_id=workspace_id,
            reason="credit metadata row not found",
        )
    if raw.get("workspace_id") != workspace_id:
        return CreditJsonCleanupResult(
            workspace_id=workspace_id,
            reason="credit metadata workspace_id does not match row id",
        )

    legacy_fields = tuple(sorted(LEGACY_CREDIT_MONEY_FIELDS.intersection(raw)))
    try:
        shard_count = credit_shard_count(raw)
    except (TypeError, ValueError) as exc:
        return CreditJsonCleanupResult(
            workspace_id=workspace_id,
            legacy_fields=legacy_fields,
            reason=str(exc),
        )

    expected = tuple(range(shard_count))
    rows = reader.execute_sql(
        "SELECT shard FROM tr_credit_balance "
        "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count ORDER BY shard",
        params={"pk": workspace_id, "shard_count": shard_count},
        param_types={
            "pk": store._param_types.STRING,
            "shard_count": store._param_types.INT64,
        },
    )
    observed = tuple(int(row[0]) for row in rows)
    if observed != expected:
        return CreditJsonCleanupResult(
            workspace_id=workspace_id,
            legacy_fields=legacy_fields,
            expected_shards=expected,
            observed_shards=observed,
            reason="authoritative typed shard set is incomplete",
        )
    return CreditJsonCleanupResult(
        workspace_id=workspace_id,
        legacy_fields=legacy_fields,
        expected_shards=expected,
        observed_shards=observed,
        ready=True,
    )
