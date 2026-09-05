"""Provider-neutral orchestration for historical and recurring trust scans."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import AdverseTrustEvent
from trusted_router.storage_trust_reconciliation import TrustReconciliationRepository
from trusted_router.stripe_trust_history import StripeTrustScan
from trusted_router.trust_reconciliation import (
    BackfillMarker,
    OutstandingAdverse,
    canonical_mapping,
    canonical_records_from_events,
    outstanding_is_beyond_horizon,
    reconcile_canonical_mappings,
    reconciliation_tail_start,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrustReconcileResult:
    marker: BackfillMarker
    payments_seen: int
    adverse_seen: int
    outstanding_count: int
    watermark_advanced: bool


def _provider_mapping(
    scan: StripeTrustScan,
    *,
    repository: TrustReconciliationRepository,
    provider: str,
    range_start: datetime,
    range_end: datetime,
) -> tuple[dict[tuple[str, str, str], str], dict[tuple[str, str, str], str]]:
    source_records = tuple(
        record
        for record in canonical_records_from_events(scan.source_events)
        if record.provider == provider
    )
    source = canonical_mapping(source_records)
    local_records = tuple(
        record
        for record in canonical_records_from_events(
            repository.list_provider_events(provider)
        )
        if record.key in source or range_start <= record.occurred_at < range_end
    )
    return source, canonical_mapping(local_records)


def _apply_scan(
    repository: TrustReconciliationRepository,
    scan: StripeTrustScan,
    *,
    provider: str,
) -> tuple[int, int]:
    unmatched = len(scan.unmatched_ids)
    semantic_mismatches = 0
    for payment in sorted(scan.payments, key=lambda row: (row.occurred_at, row.event_id)):
        if payment.provider == provider:
            repository.write_payment_fact(payment)
    for adverse in sorted(scan.adverse, key=lambda row: (row.occurred_at, row.event_id)):
        if adverse.provider != provider:
            continue
        outcome = repository.write_adverse_fact(adverse)
        if outcome == "inbox":
            unmatched += 1
        elif outcome in {"illegal", "stale"}:
            semantic_mismatches += 1
    return unmatched, semantic_mismatches


def reconcile_scan(
    repository: TrustReconciliationRepository,
    scan: StripeTrustScan,
    *,
    provider: str,
    account_id: str,
    environment: str,
    source: str,
    source_version: str,
    history_start: datetime,
    closed_through: datetime,
    consistency_delay_seconds: int,
    now: datetime,
    previous_closed_through: datetime | None = None,
    extra_unmatched: int = 0,
    extra_semantic_mismatches: int = 0,
    outstanding_count: int = 0,
    comparison_start: datetime | None = None,
) -> TrustReconcileResult:
    """Apply through live writers, prove two-way parity, then persist marker."""

    unmatched, semantic = _apply_scan(repository, scan, provider=provider)
    source_mapping, local_mapping = _provider_mapping(
        scan,
        repository=repository,
        provider=provider,
        range_start=comparison_start or history_start,
        range_end=closed_through,
    )
    diff = reconcile_canonical_mappings(source_mapping, local_mapping)
    unmatched += diff.unmatched_count + extra_unmatched
    semantic += diff.semantic_mismatch_count + extra_semantic_mismatches
    clean = unmatched == 0 and semantic == 0
    persisted_closed_through = (
        closed_through
        if clean
        else previous_closed_through or history_start
    )
    marker = BackfillMarker(
        provider=provider,
        account_id=account_id,
        environment=environment,
        source=source,
        source_version=source_version,
        history_start=history_start,
        closed_through=persisted_closed_through,
        consistency_delay_seconds=consistency_delay_seconds,
        unmatched_count=unmatched,
        semantic_mismatch_count=semantic,
        completed_at=now if clean else None,
    )
    repository.save_marker(marker)
    log.info(
        "trust.backfill.unmatched provider=%s value=%d semantic_mismatch=%d",
        provider,
        unmatched,
        semantic,
    )
    log.info(
        "trust.reconcile.outstanding provider=%s value=%d",
        provider,
        outstanding_count,
    )
    return TrustReconcileResult(
        marker=marker,
        payments_seen=sum(row.provider == provider for row in scan.payments),
        adverse_seen=sum(row.provider == provider for row in scan.adverse),
        outstanding_count=outstanding_count,
        watermark_advanced=clean and persisted_closed_through != previous_closed_through,
    )


def run_historical_backfill(
    repository: TrustReconciliationRepository,
    scan: StripeTrustScan,
    *,
    provider: str,
    account_id: str,
    environment: str,
    source: str,
    source_version: str,
    history_start: datetime,
    closed_through: datetime,
    consistency_delay_seconds: int,
    now: datetime,
) -> TrustReconcileResult:
    if closed_through > now - timedelta(seconds=consistency_delay_seconds):
        raise ValueError("closed_through enters the provider consistency-delay window")
    return reconcile_scan(
        repository,
        scan,
        provider=provider,
        account_id=account_id,
        environment=environment,
        source=source,
        source_version=source_version,
        history_start=history_start,
        closed_through=closed_through,
        consistency_delay_seconds=consistency_delay_seconds,
        now=now,
    )


RefetchAdverse = Callable[
    [OutstandingAdverse, datetime],
    tuple[AdverseTrustEvent, OutstandingAdverse],
]
AlertHorizon = Callable[[OutstandingAdverse], None]


def run_recurring_reconciliation(
    repository: TrustReconciliationRepository,
    scan_tail: Callable[[datetime, datetime], StripeTrustScan],
    refetch_adverse: RefetchAdverse,
    *,
    provider: str,
    account_id: str,
    environment: str,
    source: str,
    source_version: str,
    cadence_seconds: int,
    now: datetime,
    alert_horizon: AlertHorizon,
) -> TrustReconcileResult:
    marker = repository.get_marker(
        provider, account_id, environment, source, source_version
    )
    if marker is None:
        raise RuntimeError("historical trust marker is absent; recurring pass refused")
    tail_start = reconciliation_tail_start(
        marker.closed_through,
        consistency_delay_seconds=marker.consistency_delay_seconds,
        cadence_seconds=cadence_seconds,
    )
    tail_end = now.astimezone(UTC) - timedelta(
        seconds=marker.consistency_delay_seconds
    )
    scan = scan_tail(tail_start, tail_end)
    failed_refetches = 0
    refetch_semantic_mismatches = 0
    outstanding = repository.list_outstanding(provider)
    for row in outstanding:
        try:
            event, refreshed = refetch_adverse(row, now)
            if event.adverse_ref != row.adverse_ref or event.kind != row.kind:
                refetch_semantic_mismatches += 1
                continue
            if outstanding_is_beyond_horizon(refreshed, now=now):
                alert_horizon(refreshed)
                event = dataclasses.replace(
                    event,
                    lifecycle_status="terminal_by_horizon",
                    provider_ordering_watermark=(
                        event.provider_ordering_watermark + ":terminal_by_horizon"
                    ),
                )
            outcome = repository.write_adverse_fact(event)
            if outcome == "inbox":
                failed_refetches += 1
            elif outcome in {"illegal", "stale"}:
                refetch_semantic_mismatches += 1
        except Exception:
            log.exception(
                "trust adverse re-fetch failed provider=%s adverse_ref=%s",
                provider,
                row.adverse_ref,
            )
            failed_refetches += 1
    return reconcile_scan(
        repository,
        scan,
        provider=provider,
        account_id=account_id,
        environment=environment,
        source=source,
        source_version=source_version,
        history_start=marker.history_start,
        closed_through=tail_end,
        consistency_delay_seconds=marker.consistency_delay_seconds,
        now=now,
        previous_closed_through=marker.closed_through,
        extra_unmatched=failed_refetches,
        extra_semantic_mismatches=refetch_semantic_mismatches,
        outstanding_count=len(outstanding),
        comparison_start=tail_start,
    )
