"""Pure trust-tier policy shared by the Spanner computation job and tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import CreditProvenance, TrustEvent

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
    identity_ceiling = 3 if approved else 1
    effective = min(identity_ceiling, max(computed, override), 3)
    if trust_latched_at is not None:
        effective = 0
    return TrustTierDecision(computed_tier=computed, effective_tier=effective)
