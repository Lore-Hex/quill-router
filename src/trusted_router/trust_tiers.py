"""Pure trust-tier policy shared by the Spanner computation job and tests."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, TrustEvent

TRUST_EVENT_KINDS = frozenset({"payment", "refund", "dispute", "abuse", "grant"})
TRUST_EVENT_PROVIDERS = frozenset(
    {"stripe", "paypal", "adyen", "x402", "operator", "system"}
)
TRUST_EVENT_LIFECYCLE_STATUSES = frozenset(
    {
        "pending",
        "succeeded",
        "failed",
        "reversed",
        "won",
        "lost",
        "closed",
        "terminal_by_horizon",
    }
)
TRUST_EVENT_DEBIT_STATUSES = frozenset({"debited", "partial", "unrecovered"})
_PAYMENT_SOURCES = {
    "stripe": frozenset({"checkout", "auto_refill"}),
    "paypal": frozenset({"capture"}),
    "adyen": frozenset({"authorisation"}),
    "x402": frozenset({"x402"}),
}

TRUST_PAUSE_CAUSES = frozenset(
    {"abuse", "principal_recovery", "resharding", "federation", "migration"}
)

_REFUND_TRANSITIONS = {
    None: frozenset({"pending", "succeeded", "failed", "reversed"}),
    "pending": frozenset({"pending", "succeeded", "failed", "reversed"}),
    "succeeded": frozenset({"succeeded", "reversed"}),
    "failed": frozenset({"failed"}),
    "reversed": frozenset({"reversed"}),
    "terminal_by_horizon": frozenset({"terminal_by_horizon"}),
}
_DISPUTE_TRANSITIONS = {
    None: frozenset({"pending", "succeeded", "won", "lost", "closed"}),
    "pending": frozenset({"pending", "succeeded", "won", "lost", "closed"}),
    "succeeded": frozenset({"succeeded", "won", "lost", "closed"}),
    "won": frozenset({"won"}),
    "lost": frozenset({"lost", "closed"}),
    "closed": frozenset({"closed"}),
    "terminal_by_horizon": frozenset({"terminal_by_horizon"}),
}


def validate_adverse_event(event: AdverseTrustEvent) -> None:
    if event.provider not in {"stripe", "x402", "paypal", "adyen"}:
        raise ValueError("unsupported adverse provider")
    if event.kind not in {"refund", "dispute"}:
        raise ValueError("adverse trust event must be a refund or dispute")
    if not event.adverse_ref or not event.original_payment_ref:
        raise ValueError("adverse and original payment references are required")
    if event.provider in {"stripe", "x402"} and not event.original_payment_ref.startswith("pi_"):
        raise ValueError("Stripe/x402 adverse event requires a PaymentIntent id")
    if event.amount_micro < 0:
        raise ValueError("adverse amount must not be negative")
    if event.lifecycle_status not in TRUST_EVENT_LIFECYCLE_STATUSES:
        raise ValueError("unsupported adverse lifecycle status")
    if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
        raise ValueError("adverse occurred_at must be timezone-aware")
    if not event.provider_ordering_watermark:
        raise ValueError("provider ordering watermark is required")


def adverse_event_payload(event: AdverseTrustEvent) -> str:
    """Serialize the canonical observation used by transactional inbox drain."""

    return json.dumps(
        {
            "event_id": event.event_id,
            "provider": event.provider,
            "kind": event.kind,
            "adverse_ref": event.adverse_ref,
            "original_payment_ref": event.original_payment_ref,
            "amount_micro": event.amount_micro,
            "provider_subtype": event.provider_subtype,
            "lifecycle_status": event.lifecycle_status,
            "occurred_at": event.occurred_at.isoformat(),
            "provider_ordering_watermark": event.provider_ordering_watermark,
            "payload": event.payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def adverse_event_from_payload(payload: str) -> AdverseTrustEvent:
    body = json.loads(payload)
    event = AdverseTrustEvent(
        event_id=str(body["event_id"]),
        provider=str(body["provider"]),
        kind=str(body["kind"]),
        adverse_ref=str(body["adverse_ref"]),
        original_payment_ref=str(body["original_payment_ref"]),
        amount_micro=int(body["amount_micro"]),
        provider_subtype=str(body["provider_subtype"]),
        lifecycle_status=str(body["lifecycle_status"]),
        occurred_at=datetime.fromisoformat(str(body["occurred_at"])),
        provider_ordering_watermark=str(body["provider_ordering_watermark"]),
        payload=str(body.get("payload") or ""),
    )
    validate_adverse_event(event)
    return event


def adverse_transition_outcome(
    *,
    kind: str,
    old_status: str | None,
    old_watermark: str | None,
    new_status: str,
    new_watermark: str,
) -> str:
    """Classify one lifecycle observation without applying money."""

    if old_status == new_status:
        return "replay"
    if old_watermark is not None and new_watermark <= old_watermark:
        return "stale"
    graph = _REFUND_TRANSITIONS if kind == "refund" else _DISPUTE_TRANSITIONS
    if new_status not in graph.get(old_status, frozenset()):
        return "illegal"
    return "applied"


def payment_recovery_target(payment: TrustEvent, adverse: Iterable[TrustEvent]) -> tuple[int, int]:
    """Return (target, net_refunded) from aggregate current adverse state."""

    credited = int(payment.credited_micro or 0)
    payment_amount = int(payment.payment_amount_micro or 0)
    if payment_amount <= 0:
        raise ValueError("payment fact has no positive payment amount")
    rows = list(adverse)
    net_refunded = min(
        payment_amount,
        sum(
            int(row.amount_micro or 0)
            for row in rows
            if row.kind == "refund" and row.lifecycle_status == "succeeded"
        ),
    )
    refund_target = credited * net_refunded // payment_amount
    dispute_claims_all = any(
        row.kind == "dispute" and row.lifecycle_status in {"succeeded", "lost", "closed"}
        for row in rows
    )
    return (credited if dispute_claims_all else refund_target), net_refunded


@dataclass(frozen=True, slots=True)
class TrustTierDecision:
    computed_tier: int
    effective_tier: int


def validate_credit_provenance(
    *,
    source: str,
    provider: str,
    external_ref: str | None,
    occurred_at: datetime,
) -> None:
    if provider not in TRUST_EVENT_PROVIDERS:
        raise ValueError("unsupported trust-event provider")
    if not source.strip():
        raise ValueError("credit provenance source is required")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("credit provenance occurred_at must be timezone-aware")
    if provider in {"stripe", "paypal", "adyen", "x402"} and not external_ref:
        raise ValueError("payment credit provenance requires a provider object reference")
    if provider in _PAYMENT_SOURCES and source not in _PAYMENT_SOURCES[provider]:
        raise ValueError("payment credit provenance source does not match its provider")
    if provider in {"stripe", "x402"} and not str(external_ref).startswith("pi_"):
        raise ValueError("Stripe/x402 credit provenance requires a PaymentIntent id")
    if provider == "paypal" and str(external_ref).startswith("WH-"):
        raise ValueError("PayPal credit provenance requires a capture id, not a webhook id")
    if provider in {"operator", "system"}:
        if source not in {"grant", "provisioning"}:
            raise ValueError("operator/system credit source must be grant or provisioning")
        if external_ref is not None:
            raise ValueError("operator/system credit must not carry an external reference")


def payment_or_grant_event(
    workspace_id: str,
    event_id: str,
    amount_microdollars: int,
    provenance: CreditProvenance,
    *,
    recorded_at: datetime,
    payment_amount_microdollars: int | None = None,
    currency: str | None = None,
) -> TrustEvent:
    validate_credit_provenance(
        source=provenance.source,
        provider=provenance.provider,
        external_ref=provenance.external_ref,
        occurred_at=provenance.occurred_at,
    )
    is_payment = provenance.provider in {"stripe", "paypal", "adyen", "x402"}
    credited = int(amount_microdollars)
    payment_amount = (
        int(payment_amount_microdollars)
        if payment_amount_microdollars is not None
        else credited
    )
    if is_payment and payment_amount <= 0:
        raise ValueError("payment amount must be positive")
    normalized_currency = currency.upper() if currency else None
    if is_payment and (payment_amount_microdollars is None or normalized_currency is None):
        raise ValueError("payment amount and currency are required for payment facts")
    return TrustEvent(
        workspace_id=workspace_id,
        event_id=event_id,
        kind="payment" if is_payment else "grant",
        provider=provenance.provider,
        amount_micro=payment_amount if is_payment else credited,
        original_payment_ref=provenance.external_ref,
        adverse_ref=None,
        occurred_at=provenance.occurred_at,
        recorded_at=recorded_at,
        payment_amount_micro=payment_amount if is_payment else None,
        currency=normalized_currency if is_payment else None,
        credited_micro=credited,
        recovered_micro=0 if is_payment else None,
        provider_subtype=provenance.source,
        lifecycle_status="succeeded",
        cumulative_refunded=0 if is_payment else None,
        recovery_target=0 if is_payment else None,
        debit_status=None,
        unrecovered_micro=0 if is_payment else None,
        provider_ordering_watermark=None,
    )


def compute_trust_tier(
    events: Iterable[TrustEvent],
    *,
    owner_identity_status: str,
    trust_latched_at: datetime | None,
    trust_override_tier: int | None,
    qualifying_providers: frozenset[str],
    tier3_min_days: int,
    tier3_min_paid_microdollars: int,
    now: datetime,
    identity_bypass: bool = False,
) -> TrustTierDecision:
    """Compute the converged tier without mutating or clearing a latch."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    qualifying = [
        event
        for event in events
        if event.kind == "payment"
        and event.provider in qualifying_providers
        and event.lifecycle_status == "succeeded"
    ]
    approved = owner_identity_status == "approved"
    computed = 0
    if qualifying:
        computed = 2 if approved else 1
        first_payment_at = min(event.occurred_at for event in qualifying)
        total_paid = sum(int(event.payment_amount_micro or 0) for event in qualifying)
        has_adverse_history = any(
            event.kind in {"refund", "dispute"} for event in events
        )
        if (
            approved
            and first_payment_at <= now.astimezone(UTC) - timedelta(days=tier3_min_days)
            and total_paid >= tier3_min_paid_microdollars
            and not has_adverse_history
        ):
            computed = 3

    override = max(0, min(3, int(trust_override_tier or 0)))
    identity_ceiling = 3 if approved or identity_bypass else 1
    effective = min(identity_ceiling, max(computed, override), 3)
    if trust_latched_at is not None:
        effective = 0
    return TrustTierDecision(computed_tier=computed, effective_tier=effective)


# Slice 1d adds the horizon terminal as an allowed reconciler transition. Kept
# at the end so the independently authored owner/override slice can merge its
# additions without touching the existing transition literals.
_payment_recovery_target_without_horizon = payment_recovery_target


def _payment_recovery_target_with_horizon(
    payment: TrustEvent,
    adverse: Iterable[TrustEvent],
) -> tuple[int, int]:
    rows = tuple(adverse)
    target, net_refunded = _payment_recovery_target_without_horizon(payment, rows)
    credited = int(payment.credited_micro or 0)
    # Horizon terminalization is observational, not a won outcome. Preserve a
    # claim that was already active without turning a warning/pending dispute
    # into a new monetary claim.
    if any(
        row.kind == "dispute"
        and row.lifecycle_status == "terminal_by_horizon"
        and int(row.recovery_target or 0) >= credited
        for row in rows
    ):
        return credited, net_refunded
    return target, net_refunded


payment_recovery_target = _payment_recovery_target_with_horizon

_REFUND_TRANSITIONS["pending"] = _REFUND_TRANSITIONS["pending"] | {
    "terminal_by_horizon"
}
_DISPUTE_TRANSITIONS["pending"] = _DISPUTE_TRANSITIONS["pending"] | {
    "terminal_by_horizon"
}
_DISPUTE_TRANSITIONS["succeeded"] = _DISPUTE_TRANSITIONS["succeeded"] | {
    "terminal_by_horizon"
}


def trust_reconciliation_is_fresh(
    reconciled_through: datetime | None,
    *,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    """Pure PR-2 mint-guard seam; slice 1d deliberately does not call it."""

    from trusted_router.trust_reconciliation import reconciliation_is_fresh

    return reconciliation_is_fresh(
        reconciled_through,
        now=now,
        max_age_seconds=max_age_seconds,
    )


def trust_inbox_reference(event: AdverseTrustEvent) -> str:
    """Keep every provider lifecycle observation until its payment is visible.

    Fact dedup remains (provider, adverse_ref, kind). The inbox is a delivery
    journal: a pending observation must not swallow a later completion.
    """
    if event.provider not in {"paypal", "adyen"}:
        return event.adverse_ref
    import hashlib

    digest = hashlib.sha256(adverse_event_payload(event).encode()).hexdigest()[:24]
    return f"{event.adverse_ref}:{digest}"
