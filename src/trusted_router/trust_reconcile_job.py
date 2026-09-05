"""Provider-neutral orchestration for historical and recurring trust scans."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import AdverseTrustEvent, TrustEvent
from trusted_router.storage_trust_reconciliation import TrustReconciliationRepository
from trusted_router.stripe_trust_history import StripeTrustScan
from trusted_router.synthetic.alerts import ops_alert
from trusted_router.trust_reconciliation import (
    BackfillMarker,
    OutstandingAdverse,
    _derived_targets,
    canonical_mapping,
    canonical_records_from_events,
    outstanding_is_beyond_horizon,
    reconcile_canonical_mappings,
    reconciliation_tail_start,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrustReconcileResult:
    #: The marker this pass computed. Persisted unless ``marker_saved`` is
    #: False: an unclean recurring tick reports its counts here but leaves the
    #: last clean marker in place (see ``reconcile_scan``).
    marker: BackfillMarker
    payments_seen: int
    adverse_seen: int
    outstanding_count: int
    watermark_advanced: bool
    uncredited_payment_refs: tuple[str, ...] = ()
    marker_saved: bool = True


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
    # Payments are listed by PaymentIntent.created and stamped from a scan-
    # visible created time, so a local payment inside the window must appear
    # in the source. Adverse facts are stamped occurred_at=Event.created and the
    # live writer moves that on every status change, while the scan lists
    # refunds/disputes by the OBJECT's created: an old pending refund that
    # succeeds inside this window would be local-only by clock alone. Adverse
    # completeness is proven the other way (every listed refund/dispute needs
    # a local key; non-terminal ones are re-fetched), so local adverse facts
    # join the diff only when the source lists them (P1 review, finding 9).
    local_records = tuple(
        record
        for record in canonical_records_from_events(
            repository.list_provider_events(provider)
        )
        if record.key in source
        or (record.kind == "payment" and range_start <= record.occurred_at < range_end)
    )
    return source, canonical_mapping(local_records)


def _alert_uncredited_payment(payment: TrustEvent, *, provider: str) -> None:
    payment_ref = str(payment.original_payment_ref)
    log.warning(
        "trust.backfill.uncredited_payment provider=%s payment_ref=%s workspace_id=%s",
        provider,
        payment_ref,
        payment.workspace_id,
    )
    ops_alert(
        "trust.backfill.uncredited_payment "
        f"provider={provider} payment_ref={payment_ref} workspace_id={payment.workspace_id}",
        fingerprint=["trust.backfill.uncredited_payment", provider, payment_ref],
        tags={"provider": provider, "payment_ref": payment_ref},
    )


def _apply_scan(
    repository: TrustReconciliationRepository,
    scan: StripeTrustScan,
    *,
    provider: str,
    write_payments: bool,
) -> tuple[int, int, tuple[str, ...]]:
    """Apply a scan through the live writers.

    Payment facts are written only by the historical backfill
    (``write_payments=True``) and only when the repository finds local credit
    evidence in the same transaction; an uncredited PaymentIntent counts as
    unmatched and pages. The recurring pass compares payments and applies
    adverse facts only: the crediting webhook is the sole writer of a live
    payment fact, so a scan can never pre-empt it.
    """

    unmatched = len(scan.unmatched_ids)
    semantic_mismatches = 0
    uncredited: list[str] = []
    if write_payments:
        for payment in sorted(scan.payments, key=lambda row: (row.occurred_at, row.event_id)):
            if payment.provider != provider:
                continue
            payment_ref = str(payment.original_payment_ref)
            outcome = repository.write_payment_fact(
                payment,
                evidence_ids=tuple(scan.credit_evidence.get(payment_ref, ())),
            )
            if outcome == "uncredited":
                unmatched += 1
                uncredited.append(payment_ref)
                _alert_uncredited_payment(payment, provider=provider)
    for adverse in sorted(scan.adverse, key=lambda row: (row.occurred_at, row.event_id)):
        if adverse.provider != provider:
            continue
        outcome = repository.write_adverse_fact(adverse)
        if outcome == "inbox":
            unmatched += 1
        elif outcome in {"illegal", "stale"}:
            semantic_mismatches += 1
    return unmatched, semantic_mismatches, tuple(uncredited)


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
    write_payments: bool = True,
) -> TrustReconcileResult:
    """Apply through live writers, prove two-way parity, then persist marker.

    Marker persistence: the historical backfill (``write_payments=True``)
    always persists, so an unclean backfill is visible as ``completed_at
    NULL`` with ``closed_through=history_start`` and the recurring pass refuses
    it (P1-E). The recurring pass persists only a clean tick: an unclean tick
    (a tail webhook still being retried, a failed re-fetch, an adverse fact in
    the inbox) is transient, so it reports its counts, alerts through the CLI
    and leaves the last clean marker untouched. Fail-closed comes from
    ``trust_reconciled_through`` staleness (TR_TRUST_RECONCILE_MAX_AGE_SECONDS)
    while the next tick re-covers exactly the same tail; overwriting the marker
    with ``completed_at NULL`` would instead latch the job until an operator
    re-ran the full historical backfill (P1 review, findings 3/5/8).
    """

    unmatched, semantic, uncredited = _apply_scan(
        repository, scan, provider=provider, write_payments=write_payments
    )
    source_mapping, local_mapping = _provider_mapping(
        scan,
        repository=repository,
        provider=provider,
        range_start=comparison_start or history_start,
        range_end=closed_through,
    )
    diff = reconcile_canonical_mappings(source_mapping, local_mapping)
    # An uncredited PaymentIntent is already counted once above; its key is
    # also source-only in the two-way diff. Count each missing payment once.
    source_only_uncredited = sum(
        key[0] == provider and key[1] == "payment" and key[2] in uncredited
        for key in diff.source_only
    )
    unmatched += diff.unmatched_count - source_only_uncredited + extra_unmatched
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
    marker_saved = clean or write_payments
    if marker_saved:
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
        uncredited_payment_refs=uncredited,
        marker_saved=marker_saved,
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
        write_payments=True,
    )


@dataclass(frozen=True, slots=True)
class PaymentPlan:
    provider: str
    payment_ref: str
    workspace_id: str
    occurred_at: datetime
    payment_amount_micro: int
    credited_micro: int
    currency: str | None
    local_fact: bool
    credit_evidence: tuple[str, ...]
    credited_locally: bool
    action: str  # "write" | "already_present" | "refuse_uncredited"


@dataclass(frozen=True, slots=True)
class AdversePlan:
    provider: str
    kind: str
    adverse_ref: str
    payment_ref: str
    workspace_id: str | None
    lifecycle_status: str
    amount_micro: int
    action: str  # "apply" | "replay" | "inbox"
    recovery_target_micro: int
    recovery_debit_micro: int
    latch_implied: bool


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    provider: str
    payments: tuple[PaymentPlan, ...]
    adverse: tuple[AdversePlan, ...]
    unmatched_ids: tuple[str, ...]

    @property
    def uncredited_count(self) -> int:
        return sum(row.action == "refuse_uncredited" for row in self.payments)


def plan_historical_backfill(
    repository: TrustReconciliationRepository,
    scan: StripeTrustScan,
    *,
    provider: str,
) -> BackfillPlan:
    """Read-only preview of what ``run_historical_backfill`` would write.

    Per PaymentIntent: whether it is credited locally (a stored fact whose
    ``stripe_event`` marker exists, or derivable evidence) and the action the
    apply run would take. Per refund/dispute: the transition, the recovery
    debit the writer would take (derived target minus what is already
    recovered) and whether the workspace latch is implied. Nothing is written.
    """

    local_events = repository.list_provider_events(provider)
    local_payments = {
        str(row.original_payment_ref): row
        for row in local_events
        if row.kind == "payment" and row.original_payment_ref
    }
    local_adverse = {
        str(row.adverse_ref): row for row in local_events if row.kind != "payment" and row.adverse_ref
    }
    payments: list[PaymentPlan] = []
    source_payments: dict[str, TrustEvent] = {}
    for payment in sorted(scan.payments, key=lambda row: (row.occurred_at, row.event_id)):
        if payment.provider != provider:
            continue
        payment_ref = str(payment.original_payment_ref)
        source_payments[payment_ref] = payment
        existing = local_payments.get(payment_ref)
        evidence = tuple(scan.credit_evidence.get(payment_ref, ()))
        probe = evidence if existing is None else tuple(dict.fromkeys((*evidence, existing.event_id)))
        credited_locally = repository.has_credit_evidence(probe)
        if existing is not None:
            action = "already_present"
        elif credited_locally:
            action = "write"
        else:
            action = "refuse_uncredited"
        payments.append(
            PaymentPlan(
                provider=provider,
                payment_ref=payment_ref,
                workspace_id=payment.workspace_id,
                occurred_at=payment.occurred_at,
                payment_amount_micro=int(payment.payment_amount_micro or 0),
                credited_micro=int(payment.credited_micro or 0),
                currency=payment.currency,
                local_fact=existing is not None,
                credit_evidence=evidence,
                credited_locally=credited_locally,
                action=action,
            )
        )
    adverse_plans: list[AdversePlan] = []
    source_adverse = {
        str(row.adverse_ref): row
        for row in scan.source_events
        if row.kind != "payment" and row.adverse_ref
    }
    for adverse in sorted(scan.adverse, key=lambda row: (row.occurred_at, row.event_id)):
        if adverse.provider != provider:
            continue
        paid: TrustEvent | None = local_payments.get(
            adverse.original_payment_ref
        ) or source_payments.get(adverse.original_payment_ref)
        existing_adverse = local_adverse.get(adverse.adverse_ref)
        if paid is None:
            action = "inbox"
        elif (
            existing_adverse is not None
            and existing_adverse.lifecycle_status == adverse.lifecycle_status
        ):
            action = "replay"
        else:
            action = "apply"
        target = 0
        debit = 0
        if paid is not None:
            siblings = [
                row
                for row in local_events
                if row.kind != "payment"
                and row.original_payment_ref == adverse.original_payment_ref
                and row.adverse_ref != adverse.adverse_ref
            ]
            projected = source_adverse.get(adverse.adverse_ref)
            rows = [paid, *siblings, *([projected] if projected is not None else [])]
            target = _derived_targets(rows).get(
                (provider, str(adverse.original_payment_ref)), 0
            )
            recovered = int(paid.recovered_micro or 0)
            debit = max(0, target - recovered) if action == "apply" else 0
        adverse_plans.append(
            AdversePlan(
                provider=provider,
                kind=adverse.kind,
                adverse_ref=adverse.adverse_ref,
                payment_ref=adverse.original_payment_ref,
                workspace_id=paid.workspace_id if paid is not None else None,
                lifecycle_status=adverse.lifecycle_status,
                amount_micro=adverse.amount_micro,
                action=action,
                recovery_target_micro=target,
                recovery_debit_micro=debit,
                latch_implied=action == "apply",
            )
        )
    return BackfillPlan(
        provider=provider,
        payments=tuple(payments),
        adverse=tuple(adverse_plans),
        unmatched_ids=tuple(scan.unmatched_ids),
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
    if marker.completed_at is None or not marker.is_complete:
        # Only the historical backfill persists an incomplete marker (an
        # unclean recurring tick leaves the last clean one in place, see
        # reconcile_scan), and it does so with closed_through=history_start.
        # Rescanning from there every tick would re-list the whole history and
        # re-page on every PaymentIntent it already refused; the fix is a clean
        # re-run of the historical job, never the recurring pass.
        raise RuntimeError(
            "historical trust marker is incomplete (completed_at IS NULL); "
            "recurring pass refused"
        )
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
        write_payments=False,
    )
