"""Auto-refill: when a workspace's available balance drops below the
configured threshold, charge the saved Stripe payment method off-session
and credit the workspace via the existing webhook path.

Trigger surface: `maybe_charge_after_settle(workspace_id)` should be
called after every successful `STORE.settle(...)`. It's a no-op for
workspaces with auto_refill disabled, and a no-op while another refill
is in flight (idempotency-key gated).

The actual credit happens via the `payment_intent.succeeded` webhook on
`/internal/stripe/webhook` — this function only kicks off the charge.
That keeps the credit ledger update behind the same idempotent code
path that one-off Checkout uses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from trusted_router.config import Settings
from trusted_router.storage import STORE
from trusted_router.storage_models import CreditAccount
from trusted_router.typed_balance import typed_aware_credit_account

log = logging.getLogger(__name__)

# Minimum interval between failed attempts so a single bad card can't generate
# an infinite stream of declines. 5 minutes is a balance between "user expects
# refill within seconds" and "Stripe's per-customer rate limit".
MIN_RETRY_INTERVAL_SECONDS = 5 * 60

# A successfully created off-session PaymentIntent is marked pending until the
# Stripe webhook credits the workspace. During that window, creating another
# PaymentIntent can double-charge a customer even though the first charge is
# merely waiting for webhook delivery, so block duplicate auto-refills while the
# pending attempt is fresh. If the webhook is lost for long enough, allow a later
# low-balance settle to try again instead of wedging auto-refill forever.
PENDING_RETRY_INTERVAL_SECONDS = 30 * 60


@dataclass(frozen=True)
class AutoRefillOutcome:
    fired: bool
    reason: str  # "charged" | "disabled" | "above_threshold" | "pending" | "rate_limited" | "stripe_error:<code>"
    payment_intent_id: str | None = None


def maybe_charge_after_settle(
    workspace_id: str,
    *,
    settings: Settings,
) -> AutoRefillOutcome:
    """Check the workspace's auto-refill state and fire a charge if due.

    Returns a structured outcome so callers can log without re-deriving
    the decision. Never raises — Stripe / network failures are surfaced
    via `last_auto_refill_status` so retries are bounded by the next
    settle event."""
    # Typed-aware: for a typed workspace the authoritative usage/reserved live in
    # the typed table; reading stale JSON here would overstate available and the
    # threshold would never trip → the card never charges (underbill).
    account = typed_aware_credit_account(STORE, workspace_id, settings=settings)
    if account is None:
        return AutoRefillOutcome(fired=False, reason="no_account")
    if not account.auto_refill_enabled:
        return AutoRefillOutcome(fired=False, reason="disabled")
    if account.auto_refill_amount_microdollars <= 0:
        return AutoRefillOutcome(fired=False, reason="disabled")
    available = (
        account.total_credits_microdollars
        - account.total_usage_microdollars
        - account.reserved_microdollars
    )
    # The product promise is "when balance drops below the threshold".
    # Equal-to-threshold is still not below, so do not charge yet.
    if available >= account.auto_refill_threshold_microdollars:
        return AutoRefillOutcome(fired=False, reason="above_threshold")
    if not account.stripe_customer_id or not account.stripe_payment_method_id:
        return AutoRefillOutcome(fired=False, reason="no_payment_method")
    recent_attempt_reason = _recent_attempt_block_reason(account)
    if recent_attempt_reason is not None:
        return AutoRefillOutcome(fired=False, reason=recent_attempt_reason)
    if not settings.stripe_secret_key:
        return AutoRefillOutcome(fired=False, reason="stripe_not_configured")

    try:
        import stripe
    except ImportError:  # pragma: no cover - stripe is in dependencies.
        return AutoRefillOutcome(fired=False, reason="stripe_not_installed")

    stripe.api_key = settings.stripe_secret_key
    cents = max(50, account.auto_refill_amount_microdollars // 10_000)
    idempotency_key = (
        f"auto-refill:{workspace_id}:{cents}:"
        f"{datetime.now(UTC).strftime('%Y%m%d%H%M')}"
    )
    try:
        intent = stripe.PaymentIntent.create(
            amount=cents,
            currency="usd",
            customer=account.stripe_customer_id,
            payment_method=account.stripe_payment_method_id,
            off_session=True,
            confirm=True,
            description="TrustedRouter auto-refill",
            metadata={
                "workspace_id": workspace_id,
                "auto_refill": "true",
                "amount_microdollars": str(account.auto_refill_amount_microdollars),
            },
            idempotency_key=idempotency_key,
        )
    except stripe.CardError as exc:
        STORE.record_auto_refill_outcome(workspace_id, status=f"failed:{exc.code or 'card_error'}")
        log.warning("auto_refill.card_error workspace=%s code=%s", workspace_id, exc.code)
        return AutoRefillOutcome(fired=False, reason=f"stripe_error:{exc.code or 'card_error'}")
    except Exception as exc:  # noqa: BLE001 - any Stripe error blocks the refill.
        STORE.record_auto_refill_outcome(workspace_id, status="failed:network")
        log.exception("auto_refill.error workspace=%s", workspace_id)
        return AutoRefillOutcome(fired=False, reason=f"stripe_error:{type(exc).__name__}")

    STORE.record_auto_refill_outcome(workspace_id, status="pending")
    return AutoRefillOutcome(fired=True, reason="charged", payment_intent_id=intent.id)


def _recent_attempt_block_reason(account: CreditAccount) -> str | None:
    """Skip if a recent attempt is still pending or recently failed.

    We don't gate successes the same way: the successful webhook advances the
    credit balance and naturally takes the workspace out of the threshold band.
    """
    if not account.last_auto_refill_at:
        return None
    status = account.last_auto_refill_status or ""
    if status != "pending" and not status.startswith("failed:"):
        return None
    try:
        last = datetime.fromisoformat(account.last_auto_refill_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - last).total_seconds()
    if status == "pending" and age_seconds < PENDING_RETRY_INTERVAL_SECONDS:
        return "pending"
    if status.startswith("failed:") and age_seconds < MIN_RETRY_INTERVAL_SECONDS:
        return "rate_limited"
    return None
