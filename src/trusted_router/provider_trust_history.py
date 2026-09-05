"""PayPal/Adyen source identity, completion policy and shared scan conversion."""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from datetime import datetime

from trusted_router.storage_models import AdverseTrustEvent, TrustEvent
from trusted_router.stripe_trust_history import StripeTrustScan
from trusted_router.trust_reconciliation import (
    BackfillMarker,
    MarkerRequirement,
    OutstandingAdverse,
    adverse_is_terminal,
    completed_marker_satisfies,
)
from trusted_router.trust_tiers import adverse_transition_outcome

PROVIDER_SOURCES = {
    "paypal": ("paypal-transaction-search", "paypal-trust-v1", 10_800),
    "adyen": ("adyen-payment-accounting-report", "adyen-trust-v1", 0),
}


def provider_marker_qualifies(
    marker: BackfillMarker | None, *, provider: str, account_id: str,
    environment: str, payment_occurred_at: datetime | None = None,
) -> bool:
    """Pure PR-2 seam. Call for every configured account/environment.

    The optional payment bound excludes captures outside the enumerable history
    (in particular PayPal's three-year retention). This does not arm eligibility.
    """
    if provider not in PROVIDER_SOURCES or not account_id or not environment:
        return False
    source, version, delay = PROVIDER_SOURCES[provider]
    if not completed_marker_satisfies(
        marker, MarkerRequirement(provider, account_id, environment, source, version)
    ):
        return False
    assert marker is not None
    return (
        marker.consistency_delay_seconds == delay
        and marker.history_start < marker.closed_through
        and (payment_occurred_at is None or marker.history_start <= payment_occurred_at)
    )


def provider_scan(
    payments: Iterable[TrustEvent], observations: Iterable[AdverseTrustEvent], *,
    recorded_at: datetime, unmatched_ids: Iterable[str] = (),
) -> StripeTrustScan:
    """Use 1d's scan contract; source hashes are independent of stored balances."""
    payment_rows = {(p.provider, p.original_payment_ref): p for p in payments}
    adverse = tuple(sorted(observations, key=lambda e: (e.provider_ordering_watermark, e.event_id)))
    latest: dict[tuple[str, str], AdverseTrustEvent] = {}
    unmatched = list(unmatched_ids)
    for event in adverse:
        key = (event.provider, event.adverse_ref)
        old = latest.get(key)
        if old is not None:
            outcome = adverse_transition_outcome(
                kind=event.kind, old_status=old.lifecycle_status,
                old_watermark=old.provider_ordering_watermark,
                new_status=event.lifecycle_status, new_watermark=event.provider_ordering_watermark,
            )
            if outcome in {"illegal", "stale"}:
                unmatched.append(f"illegal-source-transition:{event.adverse_ref}")
                continue
            if outcome == "replay":
                if old != event:
                    # Duplicate exports may repeat an identical observation;
                    # conflicting amount/reference at one status is not proof.
                    if (old.amount_micro, old.original_payment_ref) != (event.amount_micro, event.original_payment_ref):
                        unmatched.append(f"conflicting-source:{event.adverse_ref}")
                continue
        latest[key] = event
    source_adverse: list[TrustEvent] = []
    outstanding: list[OutstandingAdverse] = []
    for event in latest.values():
        payment = payment_rows.get((event.provider, event.original_payment_ref))
        if payment is None:
            unmatched.append(event.adverse_ref)
            continue
        source_adverse.append(dataclasses.replace(
            payment, event_id=event.event_id, kind=event.kind, amount_micro=event.amount_micro,
            adverse_ref=event.adverse_ref, occurred_at=event.occurred_at,
            recorded_at=recorded_at, provider_subtype=event.provider_subtype,
            lifecycle_status=event.lifecycle_status, recovered_micro=None,
            provider_ordering_watermark=event.provider_ordering_watermark,
        ))
        if not adverse_is_terminal(event.kind, event.lifecycle_status):
            outstanding.append(OutstandingAdverse(
                provider=event.provider, kind=event.kind, adverse_ref=event.adverse_ref,
                original_payment_ref=event.original_payment_ref,
                lifecycle_status=event.lifecycle_status, occurred_at=event.occurred_at,
            ))
    return StripeTrustScan(tuple(payment_rows.values()), adverse,
                           tuple([*payment_rows.values(), *source_adverse]),
                           tuple(outstanding), tuple(sorted(set(unmatched))))
