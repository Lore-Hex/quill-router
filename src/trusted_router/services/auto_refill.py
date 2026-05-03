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

log = logging.getLogger(__name__)

# Minimum interval between attempts so a single bad card can't generate
# an infinite stream of declines. 5 minutes is a balance between "user
# expects refill within seconds" and "Stripe's per-customer rate limit".
MIN_RETRY_INTERVAL_SECONDS = 5 * 60


@dataclass(frozen=True)
class AutoRefillOutcome:
    fired: bool
    reason: str  # "charged" | "disabled" | "above_threshold" | "no_payment_method" | "rate_limited" | "stripe_error:<code>"
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
    account = STORE.get_credit_account(workspace_id)
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
    if available > account.auto_refill_threshold_microdollars:
        return AutoRefillOutcome(fired=False, reason="above_threshold")
    if not account.stripe_customer_id or not account.stripe_payment_method_id:
        return AutoRefillOutcome(fired=False, reason="no_payment_method")
    if _too_soon_to_retry(account):
        return AutoRefillOutcome(fired=False, reason="rate_limited")
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


def _too_soon_to_retry(account: CreditAccount) -> bool:
    """Skip if the last attempt failed less than MIN_RETRY_INTERVAL_SECONDS
    ago. We don't gate successes the same way — those advance the credit
    balance and naturally take the workspace out of the threshold band."""
    if not account.last_auto_refill_at:
        return False
    if account.last_auto_refill_status and account.last_auto_refill_status.startswith("failed:"):
        try:
            last = datetime.fromisoformat(account.last_auto_refill_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last).total_seconds() < MIN_RETRY_INTERVAL_SECONDS
    return False
