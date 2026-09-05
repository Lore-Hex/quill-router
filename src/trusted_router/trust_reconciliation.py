"""Pure completeness and freshness policy for payment trust reconciliation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import TrustEvent

STRIPE_CONSISTENCY_DELAY_SECONDS = 15 * 60
STRIPE_TRUST_SOURCE = "stripe-created-lists"
STRIPE_TRUST_SOURCE_VERSION = "stripe-trust-v1"
# Owner-inventory marker identity. Every marker writer and every
# MarkerRequirement reads these constants; a byte difference in any of them
# makes the marker invisible to the arm gate forever, so nothing spells them
# inline.
OWNER_INVENTORY_PROVIDER = "owner_inventory"
OWNER_INVENTORY_ACCOUNT_ID = "local"
OWNER_INVENTORY_SOURCE = "tr_entities.workspace"
OWNER_INVENTORY_SOURCE_VERSION = "owner-inventory-v1"
REFUND_HORIZON = timedelta(days=30)
DISPUTE_HORIZON_AFTER_EVIDENCE = timedelta(days=90)
TERMINAL_REFUND_STATUSES = frozenset({"succeeded", "failed", "reversed", "terminal_by_horizon"})
TERMINAL_DISPUTE_STATUSES = frozenset({"won", "lost", "closed", "terminal_by_horizon"})


@dataclass(frozen=True, slots=True)
class BackfillMarker:
    provider: str
    account_id: str
    environment: str
    source: str
    source_version: str
    history_start: datetime
    closed_through: datetime
    consistency_delay_seconds: int
    unmatched_count: int
    semantic_mismatch_count: int
    completed_at: datetime | None

    def __post_init__(self) -> None:
        for value in (self.history_start, self.closed_through, self.completed_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("trust backfill timestamps must be timezone-aware")
        if self.consistency_delay_seconds < 0:
            raise ValueError("consistency delay must not be negative")
        if self.unmatched_count < 0 or self.semantic_mismatch_count < 0:
            raise ValueError("reconciliation counts must not be negative")
        if self.completed_at is not None and not self.is_complete:
            raise ValueError("completed_at requires zero unmatched and semantic mismatch counts")

    @property
    def is_complete(self) -> bool:
        return self.unmatched_count == 0 and self.semantic_mismatch_count == 0


@dataclass(frozen=True, slots=True)
class MarkerRequirement:
    provider: str
    account_id: str
    environment: str
    source: str
    source_version: str


def completed_marker_satisfies(
    marker: BackfillMarker | None,
    requirement: MarkerRequirement,
) -> bool:
    """Pure PR-2 arm-gate predicate; row existence alone never qualifies."""

    if marker is None:
        return False
    return (
        marker.completed_at is not None
        and marker.is_complete
        and marker.provider == requirement.provider
        and marker.account_id == requirement.account_id
        and marker.environment == requirement.environment
        and marker.source == requirement.source
        and marker.source_version == requirement.source_version
    )


@dataclass(frozen=True, slots=True)
class CanonicalTrustRecord:
    provider: str
    kind: str
    provider_subtype: str | None
    adverse_ref: str | None
    original_payment_ref: str
    lifecycle_status: str | None
    payment_amount_micro: int
    currency: str
    credited_micro: int
    recovery_target: int
    workspace_id: str
    occurred_at: datetime
    provider_ordering_watermark: str | None

    @property
    def key(self) -> tuple[str, str, str]:
        canonical_id = (
            self.original_payment_ref if self.kind == "payment" else self.adverse_ref
        )
        if not canonical_id:
            raise ValueError("canonical trust record has no provider id")
        return (self.provider, self.kind, canonical_id)

    def digest(self) -> str:
        payload = dataclasses.asdict(self)
        payload["occurred_at"] = self.occurred_at.astimezone(UTC).isoformat()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ReconciliationDiff:
    source_only: tuple[tuple[str, str, str], ...]
    local_only: tuple[tuple[str, str, str], ...]
    semantic_mismatches: tuple[tuple[str, str, str], ...]

    @property
    def unmatched_count(self) -> int:
        return len(self.source_only) + len(self.local_only)

    @property
    def semantic_mismatch_count(self) -> int:
        return len(self.semantic_mismatches)

    @property
    def clean(self) -> bool:
        return self.unmatched_count == 0 and self.semantic_mismatch_count == 0


def canonical_mapping(
    records: Iterable[CanonicalTrustRecord],
) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    for record in records:
        if record.key in result:
            raise ValueError(f"duplicate canonical trust key: {record.key!r}")
        result[record.key] = record.digest()
    return result


def reconcile_canonical_mappings(
    source: Mapping[tuple[str, str, str], str],
    local: Mapping[tuple[str, str, str], str],
) -> ReconciliationDiff:
    source_keys = set(source)
    local_keys = set(local)
    common = source_keys & local_keys
    return ReconciliationDiff(
        source_only=tuple(sorted(source_keys - local_keys)),
        local_only=tuple(sorted(local_keys - source_keys)),
        semantic_mismatches=tuple(sorted(key for key in common if source[key] != local[key])),
    )


def _derived_targets(events: list[TrustEvent]) -> dict[tuple[str, str], int]:
    payments = {
        (row.provider, str(row.original_payment_ref)): row
        for row in events
        if row.kind == "payment" and row.original_payment_ref
    }
    adverse: dict[tuple[str, str], list[TrustEvent]] = defaultdict(list)
    for row in events:
        if row.kind != "payment" and row.original_payment_ref:
            adverse[(row.provider, row.original_payment_ref)].append(row)
    targets: dict[tuple[str, str], int] = {}
    for key, payment in payments.items():
        payment_amount = int(payment.payment_amount_micro or 0)
        credited = int(payment.credited_micro or 0)
        if payment_amount <= 0:
            raise ValueError(f"payment {key!r} has no positive payment amount")
        rows = adverse.get(key, [])
        net_refunded = min(
            payment_amount,
            sum(
                int(row.amount_micro or 0)
                for row in rows
                if row.kind == "refund" and row.lifecycle_status == "succeeded"
            ),
        )
        refund_target = credited * net_refunded // payment_amount
        claims_all = any(
            row.kind == "dispute"
            and (
                row.lifecycle_status in {"succeeded", "lost", "closed"}
                or (
                    row.lifecycle_status == "terminal_by_horizon"
                    and int(row.recovery_target or 0) >= credited
                )
            )
            for row in rows
        )
        targets[key] = credited if claims_all else refund_target
    return targets


def canonical_records_from_events(
    events: Iterable[TrustEvent],
) -> tuple[CanonicalTrustRecord, ...]:
    """Canonicalize stored or source facts, deriving recovery independently."""

    rows = list(events)
    targets = _derived_targets(rows)
    records: list[CanonicalTrustRecord] = []
    for row in rows:
        if row.kind not in {"payment", "refund", "dispute"}:
            continue
        original_ref = str(row.original_payment_ref or "")
        key = (row.provider, original_ref)
        if key not in targets:
            raise ValueError(f"adverse fact has no canonical payment: {key!r}")
        records.append(
            CanonicalTrustRecord(
                provider=row.provider,
                kind=row.kind,
                provider_subtype=row.provider_subtype,
                adverse_ref=row.adverse_ref,
                original_payment_ref=original_ref,
                lifecycle_status=row.lifecycle_status,
                payment_amount_micro=int(row.payment_amount_micro or 0),
                currency=str(row.currency or "").upper(),
                credited_micro=int(row.credited_micro or 0),
                recovery_target=targets[key],
                workspace_id=row.workspace_id,
                occurred_at=row.occurred_at,
                provider_ordering_watermark=row.provider_ordering_watermark,
            )
        )
    return tuple(records)


@dataclass(frozen=True, slots=True)
class OutstandingAdverse:
    provider: str
    kind: str
    adverse_ref: str
    original_payment_ref: str
    lifecycle_status: str
    occurred_at: datetime
    evidence_deadline: datetime | None = None

    @property
    def horizon_at(self) -> datetime:
        if self.kind == "refund":
            return self.occurred_at + REFUND_HORIZON
        if self.evidence_deadline is None:
            raise ValueError("a dispute horizon requires its last evidence deadline")
        return self.evidence_deadline + DISPUTE_HORIZON_AFTER_EVIDENCE


def adverse_is_terminal(kind: str, status: str) -> bool:
    terminal = TERMINAL_REFUND_STATUSES if kind == "refund" else TERMINAL_DISPUTE_STATUSES
    return status in terminal


def outstanding_is_beyond_horizon(row: OutstandingAdverse, *, now: datetime) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return not adverse_is_terminal(row.kind, row.lifecycle_status) and now >= row.horizon_at


def reconciliation_tail_start(
    closed_through: datetime,
    *,
    consistency_delay_seconds: int,
    cadence_seconds: int,
) -> datetime:
    if cadence_seconds <= 0 or consistency_delay_seconds < 0:
        raise ValueError("invalid reconciliation timing")
    return closed_through - timedelta(
        seconds=consistency_delay_seconds + 2 * cadence_seconds
    )


def reconciliation_is_fresh(
    reconciled_through: datetime | None,
    *,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    if reconciled_through is None or max_age_seconds < 0:
        return False
    if now.tzinfo is None or reconciled_through.tzinfo is None:
        raise ValueError("freshness timestamps must be timezone-aware")
    age = now.astimezone(UTC) - reconciled_through.astimezone(UTC)
    return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)
