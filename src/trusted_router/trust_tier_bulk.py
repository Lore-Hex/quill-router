"""Every workspace's trust-tier inputs from one snapshot, and the candidates.

The tier job decides each workspace with several reads and, when something
changed, a transaction. On 2026-10-02 that was 1,596 workspaces in about 13
minutes, almost all of it round trips. This module reads the same rows once,
on one strong snapshot, and runs the job's own evaluator and watermark
derivation against them through ``BulkWorkspaceReader``. That reader answers
exactly the queries those functions issue; a query it does not recognise
raises, and the workspace becomes a candidate. So a change to the evaluator
can only widen the candidate set, never narrow it.

Workspaces are read and judged a chunk at a time, all at the snapshot's
timestamp, and only digests and candidates outlive a chunk. On a generated
fleet 100 times today's (160,000 workspaces, 1.76 million balance rows, every
one a candidate), the selection grew the process by about 140 MiB, against
526 MiB when every row was held at once; the job has 512 MiB.

A workspace is a candidate when today's per-workspace path would write to it
or fail on it: its tier precheck or its watermark precheck would not skip, or
its evaluation raised.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_gcp_trust import (
    TRUST_EVENTS_SQL,
    TRUST_OVERRIDE_SQL,
    TRUST_SHARDS_SQL,
    RecordingReader,
    _trust_tier_is_current,
    evaluate_workspace_trust_tier,
)
from trusted_router.storage_trust_reconciliation import (
    MATCHING_MARKERS_SQL,
    SHARD_WATERMARKS_SQL,
    WORKSPACE_PAYMENT_PROVIDERS_SQL,
    read_expected_reconciled_through,
    same_instant,
    watermark_digest,
)
from trusted_router.trust_reconciliation import (
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
)

ENTITY_SQL = "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id"
BULK_WORKSPACE_IDS_SQL = "SELECT DISTINCT workspace_id FROM tr_credit_balance"
BULK_BALANCE_SQL = (
    "SELECT workspace_id, shard, trust_tier, trust_latched_at, trust_override_tier, "
    "trust_computed_at, trust_reconciled_through FROM tr_credit_balance "
    "WHERE workspace_id IN UNNEST(@ids)"
)
BULK_OVERRIDES_SQL = (
    "SELECT workspace_id, tier, identity_bypass FROM tr_trust_override "
    "WHERE workspace_id IN UNNEST(@ids)"
)
# TRUST_EVENTS_SQL's columns, for a list of workspaces; a test pins the match.
BULK_EVENTS_SQL = (
    "SELECT workspace_id, event_id, kind, provider, amount_micro, original_payment_ref, "
    "adverse_ref, occurred_at, recorded_at, payment_amount_micro, currency, "
    "credited_micro, recovered_micro, provider_subtype, lifecycle_status, "
    "cumulative_refunded, recovery_target, debit_status, unrecovered_micro, "
    "provider_ordering_watermark FROM tr_trust_event WHERE workspace_id IN UNNEST(@ids)"
)
BULK_MARKERS_SQL = (
    "SELECT provider, closed_through FROM tr_trust_backfill "
    "WHERE completed_at IS NOT NULL AND unmatched_count=0 AND semantic_mismatch_count=0 "
    "AND environment=@environment AND source=@source AND source_version=@source_version"
)
BULK_ENTITIES_BY_ID_SQL = "SELECT id, body FROM tr_entities WHERE kind=@kind AND id IN UNNEST(@ids)"
# Workspaces whose rows are held at once. Each chunk's rows are dropped once
# it is judged, so memory follows the chunk, not the fleet.
_CHUNK = 1_000


class UnsupportedBulkQuery(RuntimeError):
    """The evaluator issued a query the bulk snapshot cannot answer."""


@dataclass(slots=True)
class TrustTierBulk:
    """One chunk of workspaces' rows from the snapshot, grouped by workspace."""

    environment: str
    # workspace_id -> [(shard, tier, latch, override, computed_at, reconciled_through)]
    balances: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    # workspace_id -> [(tier, identity_bypass)]
    overrides: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    # workspace_id -> [TRUST_EVENTS_SQL's columns]
    events: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    # provider -> [closed_through] of the matching markers
    markers: dict[str, list[Any]] = field(default_factory=dict)
    # (kind, id) -> JSON body
    entities: dict[tuple[str, str], str] = field(default_factory=dict)


def iter_trust_tier_bulk(
    database: Any, param_types: Any, *, environment: str, chunk_size: int = _CHUNK
) -> Iterator[TrustTierBulk]:
    """Every workspace's inputs, from one strong read-only snapshot.

    Every read is at the snapshot's timestamp, so the chunks together are one
    consistent view, but only one chunk's rows are held at a time.
    """

    ids_type = param_types.Array(param_types.STRING)
    with database.snapshot(multi_use=True) as snapshot:
        markers: defaultdict[str, list[Any]] = defaultdict(list)
        for row in snapshot.execute_sql(
            BULK_MARKERS_SQL,
            params={
                "environment": environment,
                "source": STRIPE_TRUST_SOURCE,
                "source_version": STRIPE_TRUST_SOURCE_VERSION,
            },
            param_types={
                "environment": param_types.STRING,
                "source": param_types.STRING,
                "source_version": param_types.STRING,
            },
        ):
            markers[str(row[0])].append(row[1])
        workspace_ids = sorted({str(row[0]) for row in snapshot.execute_sql(BULK_WORKSPACE_IDS_SQL)})
        for start in range(0, len(workspace_ids), chunk_size):
            ids = workspace_ids[start : start + chunk_size]
            bulk = TrustTierBulk(environment=environment, markers=dict(markers))
            by_ids = {"params": {"ids": ids}, "param_types": {"ids": ids_type}}
            for sql, grouped in (
                (BULK_BALANCE_SQL, bulk.balances),
                (BULK_OVERRIDES_SQL, bulk.overrides),
                (BULK_EVENTS_SQL, bulk.events),
            ):
                for row in snapshot.execute_sql(sql, **by_ids):
                    grouped.setdefault(str(row[0]), []).append(tuple(row[1:]))
            for kind, entity_ids in (("workspace", ids), ("credit", ids)):
                _read_entities(snapshot, param_types, bulk, kind, entity_ids)
            _read_entities(snapshot, param_types, bulk, "user", sorted(_owner_ids(bulk)))
            yield bulk


def _read_entities(
    snapshot: Any, param_types: Any, bulk: TrustTierBulk, kind: str, entity_ids: list[str]
) -> None:
    if not entity_ids:
        return
    for row in snapshot.execute_sql(
        BULK_ENTITIES_BY_ID_SQL,
        params={"kind": kind, "ids": entity_ids},
        param_types={"kind": param_types.STRING, "ids": param_types.Array(param_types.STRING)},
    ):
        bulk.entities[(kind, str(row[0]))] = row[1]


def _owner_ids(bulk: TrustTierBulk) -> set[str]:
    import json

    owners: set[str] = set()
    for (kind, _id), body in bulk.entities.items():
        if kind != "workspace":
            continue
        try:
            owner = json.loads(body).get("owner_user_id")
        except (TypeError, ValueError, AttributeError):
            continue  # the evaluator reports the malformed workspace itself
        if isinstance(owner, str) and owner:
            owners.add(owner)
    return owners


class BulkWorkspaceReader:
    """Answers the evaluator's queries for one workspace from the snapshot."""

    def __init__(self, bulk: TrustTierBulk, workspace_id: str) -> None:
        self._bulk = bulk
        self._workspace_id = workspace_id

    def _own(self, params: dict[str, Any], key: str) -> None:
        if params.get(key) != self._workspace_id:
            raise UnsupportedBulkQuery(f"query for another workspace: {params.get(key)!r}")

    def answer(
        self, sql: str, params: dict[str, Any] | None = None, param_types: Any = None
    ) -> list[tuple[Any, ...]]:
        """Answer one of the evaluator's queries from the snapshot's rows."""

        params = params or {}
        bulk = self._bulk
        if sql == ENTITY_SQL:
            body = bulk.entities.get((str(params["kind"]), str(params["id"])))
            return [] if body is None else [(body,)]
        if sql == TRUST_OVERRIDE_SQL:
            self._own(params, "pk")
            return list(bulk.overrides.get(self._workspace_id, ()))
        if sql == TRUST_EVENTS_SQL:
            self._own(params, "pk")
            return list(bulk.events.get(self._workspace_id, ()))
        if sql == TRUST_SHARDS_SQL:
            self._own(params, "pk")
            count = int(params["shard_count"])
            rows = [
                row for row in bulk.balances.get(self._workspace_id, ()) if 0 <= int(row[0]) < count
            ]
            return [tuple(row[:5]) for row in sorted(rows, key=lambda row: int(row[0]))]
        if sql == WORKSPACE_PAYMENT_PROVIDERS_SQL:
            self._own(params, "workspace_id")
            providers = {
                row[2] for row in bulk.events.get(self._workspace_id, ()) if row[1] == "payment"
            }
            return [(provider,) for provider in sorted(providers)]
        if sql == MATCHING_MARKERS_SQL:
            if (
                params.get("environment") != bulk.environment
                or params.get("source") != STRIPE_TRUST_SOURCE
                or params.get("source_version") != STRIPE_TRUST_SOURCE_VERSION
            ):
                raise UnsupportedBulkQuery("marker query outside the snapshot's environment")
            return [(value,) for value in bulk.markers.get(str(params["provider"]), ())]
        if sql == SHARD_WATERMARKS_SQL:
            self._own(params, "workspace_id")
            rows = sorted(bulk.balances.get(self._workspace_id, ()), key=lambda row: int(row[0]))
            return [(row[0], row[5]) for row in rows]
        raise UnsupportedBulkQuery(sql)

    # The evaluator calls ``execute_sql``, as on a Spanner snapshot. The method
    # is defined as ``answer`` because the SQL conformance inventory treats a
    # function named ``*_sql`` as a SQL builder, and this one builds none.
    execute_sql = answer


@dataclass(slots=True)
class TrustTierSelection:
    """The workspaces today's path would act on, with the inputs they were judged on."""

    workspaces: frozenset[str]
    # workspace_id -> reasons
    candidates: dict[str, tuple[str, ...]]
    # workspace_id -> {"tier" | "tier_refused" | "watermark" | "watermark_refused": digest}
    digests: dict[str, dict[str, str]]


def select_trust_tier_candidates(
    bulks: Iterable[TrustTierBulk],
    *,
    param_types: Any,
    read_entity_tx: Any,
    qualifying_providers: frozenset[str],
    tier3_min_days: int,
    tier3_min_paid_microdollars: int,
    now: dt.datetime,
    watermark_replicated: bool = True,
) -> TrustTierSelection:
    """Judge every workspace with today's prechecks, against the snapshot.

    A refusal (an evaluation that raised on rows it read, not on a read) is
    recorded with the digest of those rows, so the shadow can match it with
    the same refusal on the per-workspace path.
    """

    workspaces: set[str] = set()
    candidates: dict[str, tuple[str, ...]] = {}
    digests: dict[str, dict[str, str]] = {}
    for bulk in bulks:
        for workspace_id in sorted(bulk.balances):
            workspaces.add(workspace_id)
            reasons: list[str] = []
            seen: dict[str, str] = {}
            reader = RecordingReader(BulkWorkspaceReader(bulk, workspace_id))
            try:
                tier, shard_rows, _count, digest = evaluate_workspace_trust_tier(
                    reader,
                    param_types=param_types,
                    read_entity_tx=read_entity_tx,
                    workspace_id=workspace_id,
                    qualifying_providers=qualifying_providers,
                    tier3_min_days=tier3_min_days,
                    tier3_min_paid_microdollars=tier3_min_paid_microdollars,
                    now=now,
                )
                seen["tier"] = digest
                if not _trust_tier_is_current(shard_rows, tier):
                    reasons.append("tier")
            except Exception as exc:  # noqa: BLE001 - any failure is the existing path's to report
                if (refused := reader.refusal_digest()) is not None:
                    seen["tier_refused"] = refused
                reasons.append(f"tier_evaluation_failed:{type(exc).__name__}")
            if watermark_replicated:
                reader = RecordingReader(BulkWorkspaceReader(bulk, workspace_id))
                try:
                    expected, digest = read_expected_reconciled_through(
                        reader,
                        param_types,
                        workspace_id,
                        qualifying_providers,
                        environment=bulk.environment,
                    )
                    current = reader.execute_sql(
                        SHARD_WATERMARKS_SQL, params={"workspace_id": workspace_id}
                    )
                    seen["watermark"] = watermark_digest(digest, current)
                    if not (current and all(same_instant(row[1], expected) for row in current)):
                        reasons.append("watermark")
                except Exception as exc:  # noqa: BLE001
                    if (refused := reader.refusal_digest()) is not None:
                        seen["watermark_refused"] = refused
                    reasons.append(f"watermark_evaluation_failed:{type(exc).__name__}")
            digests[workspace_id] = seen
            if reasons:
                candidates[workspace_id] = tuple(reasons)
    return TrustTierSelection(
        workspaces=frozenset(workspaces), candidates=candidates, digests=digests
    )
