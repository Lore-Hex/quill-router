"""Step 2 of the billing typed-column migration: backfill + drift comparator.

See docs/design/billing-typed-counters.md.

- ``compare`` is the INDEPENDENT full-row comparator (red-team P2): it scans both
  the authoritative JSON rows and the typed mirror and reports any row whose
  counters diverge (or whose typed mirror is missing). The per-write mirror is
  atomic so it cannot tear, but this comparator is the defense-in-depth that the
  Step 3 enforcement flip is gated on — flip only when it reports zero drift.

- ``backfill`` writes the typed mirror for pre-flag JSON rows (rows that existed
  before TR_TYPED_COUNTER_MIRROR was turned on). Each row is mirrored inside a
  transaction that re-reads the authoritative JSON row, so it writes a
  json-consistent typed row atomically and cannot clobber a concurrent
  dual-write with a stale read. Idempotent and re-runnable to convergence.

Both operate on a live SpannerBigtableStore (``store``), using its existing JSON
scan plus a direct typed-table scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_gcp_counters import (
    credit_drift,
    key_drift,
    mirror_write,
)

_CREDIT_TYPED_SCAN = (
    "SELECT workspace_id, total_credits, total_usage, reserved FROM tr_credit_balance"
)
_KEY_TYPED_SCAN = (
    "SELECT key_hash, limit_micro, usage, byok_usage, reserved, include_byok "
    "FROM tr_key_limit"
)


@dataclass
class DriftReport:
    credit_rows: int = 0
    key_rows: int = 0
    credit_drift: int = 0
    key_drift: int = 0
    # up to a few examples for triage: {id: {col: (json, typed)}}
    samples: dict[str, dict] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.credit_drift == 0 and self.key_drift == 0

    def summary(self) -> str:
        return (
            f"credit: {self.credit_drift}/{self.credit_rows} drift | "
            f"key: {self.key_drift}/{self.key_rows} drift | "
            f"{'CLEAN' if self.clean else 'DRIFT'}"
        )


def _scan_typed(store: Any, sql: str, key_col: str, value_cols: list[str]) -> dict[str, dict]:
    with store._database.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(sql))
    out: dict[str, dict] = {}
    for row in rows:
        record = dict(zip([key_col, *value_cols], row, strict=True))
        out[record[key_col]] = record
    return out


def compare(store: Any, *, max_samples: int = 20) -> DriftReport:
    """Scan JSON vs typed and report per-row counter drift. Read-only."""
    report = DriftReport()

    json_credit = {b["workspace_id"]: b for b in store._list_entities("credit", cls=dict)}
    typed_credit = _scan_typed(
        store, _CREDIT_TYPED_SCAN, "workspace_id",
        ["total_credits", "total_usage", "reserved"],
    )
    report.credit_rows = len(json_credit)
    for ws_id, body in json_credit.items():
        drift = credit_drift(body, typed_credit.get(ws_id))
        if drift:
            report.credit_drift += 1
            if len(report.samples) < max_samples:
                report.samples[f"credit:{ws_id}"] = drift

    json_key = {b["hash"]: b for b in store._list_entities("api_key", cls=dict)}
    typed_key = _scan_typed(
        store, _KEY_TYPED_SCAN, "key_hash",
        ["limit_micro", "usage", "byok_usage", "reserved", "include_byok"],
    )
    report.key_rows = len(json_key)
    for key_hash, body in json_key.items():
        drift = key_drift(body, typed_key.get(key_hash))
        if drift:
            report.key_drift += 1
            if len(report.samples) < max_samples:
                report.samples[f"api_key:{key_hash}"] = drift

    return report


def backfill(store: Any, *, dry_run: bool = False) -> dict[str, int]:
    """Mirror the typed row for every JSON credit/api_key row. Idempotent.

    Each row is re-read and mirrored inside one transaction so it commits a
    json-consistent typed row atomically (no stale-read clobber). Safe to run
    repeatedly; run until ``compare`` is clean.
    """
    counts = {"credit": 0, "api_key": 0}
    spanner_module = store._spanner

    plan: list[tuple[str, str]] = []
    plan += [("credit", b["workspace_id"]) for b in store._list_entities("credit", cls=dict)]
    plan += [("api_key", b["hash"]) for b in store._list_entities("api_key", cls=dict)]

    for kind, entity_id in plan:
        if dry_run:
            counts[kind] += 1
            continue

        def _txn(transaction: Any, _kind: str = kind, _id: str = entity_id) -> bool:
            body = store._read_entity_tx(transaction, _kind, _id, dict)
            if body is None:
                return False
            mirror_write(
                transaction, _kind, _id, body, spanner_module.COMMIT_TIMESTAMP
            )
            return True

        if store._run_in_transaction(_txn):
            counts[kind] += 1

    return counts
