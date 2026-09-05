"""Historical/drain-window and recurring PayPal/Adyen reconciliation."""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from trusted_router.provider_trust_history import PROVIDER_SOURCES
from trusted_router.storage_models import AdverseTrustEvent
from trusted_router.storage_trust_reconciliation import TrustReconciliationRepository
from trusted_router.stripe_trust_history import StripeTrustScan
from trusted_router.trust_reconcile_job import (
    TrustReconcileResult,
    run_historical_backfill,
    run_recurring_reconciliation,
)
from trusted_router.trust_reconciliation import OutstandingAdverse

Scan = Callable[[datetime, datetime], StripeTrustScan]
Refetch = Callable[[OutstandingAdverse, datetime], tuple[AdverseTrustEvent, OutstandingAdverse]]


class ProviderOutstandingAdverse(OutstandingAdverse):
    @property
    def horizon_at(self) -> datetime:
        # Unlike Stripe, these sources do not provide a guaranteed final
        # mutation deadline for every modification. Never manufacture one from
        # creation time: retain and re-fetch until terminal. A provider-attested
        # final deadline can be supplied in evidence_deadline.
        return self.evidence_deadline or datetime.max.replace(tzinfo=UTC)


def run_provider_backfill(
    repository: TrustReconciliationRepository, scan: Scan, *, provider: str,
    account_id: str, environment: str, history_start: datetime,
    drained_at: datetime, now: datetime,
) -> TrustReconcileResult:
    source, version, delay = PROVIDER_SOURCES[provider]
    end = now - timedelta(seconds=delay)
    if not account_id or not environment or not history_start < drained_at <= end:
        raise ValueError("Provider backfill must cover the drained revision window outside its consistency delay")
    return run_historical_backfill(
        repository, scan(history_start, end), provider=provider, account_id=account_id,
        environment=environment, source=source, source_version=version,
        history_start=history_start, closed_through=end,
        consistency_delay_seconds=delay, now=now,
    )


def run_provider_recurring(
    repository: TrustReconciliationRepository, scan: Scan, refetch: Refetch, *,
    provider: str, account_id: str, environment: str, cadence_seconds: int,
    now: datetime, alert_horizon: Callable[[OutstandingAdverse], None],
) -> TrustReconcileResult:
    source, version, delay = PROVIDER_SOURCES[provider]
    marker = repository.get_marker(provider, account_id, environment, source, version)
    if marker is None or marker.consistency_delay_seconds != delay:
        raise ValueError("Expected provider marker is absent or has the wrong delay")

    def checked_refetch(row: OutstandingAdverse, at: datetime) -> tuple[AdverseTrustEvent, OutstandingAdverse]:
        event, refreshed = refetch(row, at)
        if (event.provider, event.kind, event.adverse_ref, event.original_payment_ref) != (
            provider, row.kind, row.adverse_ref, row.original_payment_ref
        ):
            raise ValueError("Outstanding provider object identity changed")
        # Alert on long-lived work without dropping a potentially mutable id.
        if at - row.occurred_at >= timedelta(days=30):
            alert_horizon(refreshed)
        return event, ProviderOutstandingAdverse(**dataclasses.asdict(refreshed))

    return run_recurring_reconciliation(
        repository, scan, checked_refetch, provider=provider, account_id=account_id,
        environment=environment, source=source, source_version=version,
        cadence_seconds=cadence_seconds, now=now, alert_horizon=alert_horizon,
    )
