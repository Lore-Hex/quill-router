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

import json
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
    credit_orphans: int = 0  # typed rows with no JSON authority
    key_orphans: int = 0
    # up to a few examples for triage: {id: {col: (json, typed)}}
    samples: dict[str, dict] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return (
            self.credit_drift == 0
            and self.key_drift == 0
            and self.credit_orphans == 0
            and self.key_orphans == 0
        )

    def summary(self) -> str:
        return (
            f"credit: {self.credit_drift}/{self.credit_rows} drift, "
            f"{self.credit_orphans} orphan | "
            f"key: {self.key_drift}/{self.key_rows} drift, {self.key_orphans} orphan | "
            f"{'CLEAN' if self.clean else 'DRIFT'}"
        )


def _scan_json(snapshot: Any, kind: str, id_field: str, pt: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in snapshot.execute_sql(
        "SELECT body FROM tr_entities WHERE kind=@kind",
        params={"kind": kind},
        param_types={"kind": pt.STRING},
    ):
        body = json.loads(row[0])
        out[body[id_field]] = body
    return out


def compare(store: Any, *, max_samples: int = 20) -> DriftReport:
    """Scan JSON vs typed in ONE consistent snapshot and report drift + orphans.

    Read-only. A single multi-use snapshot reads JSON and typed at the same
    timestamp, so a live dual-write in flight cannot produce a transient false
    positive (codex Step-2 #3). Reports value drift, missing mirrors, AND orphan
    typed rows that have no JSON authority (codex Step-2 #1).
    """
    report = DriftReport()
    pt = store._param_types

    with store._database.snapshot(multi_use=True) as snapshot:
        json_credit = _scan_json(snapshot, "credit", "workspace_id", pt)
        json_key = _scan_json(snapshot, "api_key", "hash", pt)
        typed_credit = {
            r[0]: {"total_credits": r[1], "total_usage": r[2], "reserved": r[3]}
            for r in snapshot.execute_sql(_CREDIT_TYPED_SCAN)
        }
        typed_key = {
            r[0]: {
                "limit_micro": r[1], "usage": r[2], "byok_usage": r[3],
                "reserved": r[4], "include_byok": r[5],
            }
            for r in snapshot.execute_sql(_KEY_TYPED_SCAN)
        }

    def _sample(key: str, value: dict) -> None:
        if len(report.samples) < max_samples:
            report.samples[key] = value

    report.credit_rows = len(json_credit)
    for ws_id, body in json_credit.items():
        drift = credit_drift(body, typed_credit.get(ws_id))
        if drift:
            report.credit_drift += 1
            _sample(f"credit:{ws_id}", drift)
    for ws_id in typed_credit.keys() - json_credit.keys():
        report.credit_orphans += 1
        _sample(f"credit-orphan:{ws_id}", {"orphan_typed_row": True})

    report.key_rows = len(json_key)
    for key_hash, body in json_key.items():
        drift = key_drift(body, typed_key.get(key_hash))
        if drift:
            report.key_drift += 1
            _sample(f"api_key:{key_hash}", drift)
    for key_hash in typed_key.keys() - json_key.keys():
        report.key_orphans += 1
        _sample(f"api_key-orphan:{key_hash}", {"orphan_typed_row": True})

    return report


def backfill(store: Any, *, dry_run: bool = False) -> dict[str, int]:
    """Mirror the JSON-owned columns of the typed row for every JSON credit/
    api_key row. Idempotent; safe to run repeatedly.

    OWNERSHIP SPLIT (2026-06-25 incident) — READ BEFORE USING AS A FLIP GATE:
    this delegates to ``mirror_write``, which now writes ONLY JSON-owned columns
    (total_credits; key limit_micro/include_byok). It therefore does NOT seed the
    typed-DML-owned reserved / total_usage / usage / byok_usage, and a clean
    ``compare`` (which now audits only those JSON-owned columns) does NOT mean a
    workspace is safe to flip to typed enforcement. Flipping requires a SEPARATE
    ledger-derived reconciliation that computes reserved from open holds (legacy +
    typed) and seeds total_usage, atomic with the gate flip — otherwise the typed
    reserve gate over-admits by the sum of open/historical holds (silent
    overspend). Do NOT "fix" this by re-adding reserved/usage to the mirror: that
    full-row copy WAS the clobber.
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
