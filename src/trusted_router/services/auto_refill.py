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

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any

from trusted_router.config import Settings
from trusted_router.refill_claim import recent_block, refill_now
from trusted_router.services.stripe_fees import stripe_processing_fee
from trusted_router.storage import STORE
from trusted_router.typed_balance import live_credit_summary

log = logging.getLogger(__name__)

# Auto-refill charges the saved method off-session and books the result like
# an immediate card payment (Link wraps a card). Dynamic ("auto") Checkout can
# also be paid by bank debit, Cash App or Amazon Pay, and Checkout's
# setup_future_usage saves those too. The webhook saves only these types, and
# the off-session charge is restricted to them, so a bank account or wallet is
# never debited by an automatic top-up the customer did not choose.
AUTO_REFILL_PAYMENT_METHOD_TYPES: tuple[str, ...] = ("card", "link")


@dataclass(frozen=True)
class AutoRefillOutcome:
    fired: bool
    # "charged" | "disabled" | "threshold_not_below_amount" | "above_threshold"
    # | "pending" | "rate_limited" | "stripe_error:<code>"
    reason: str
    payment_intent_id: str | None = None
    retryable: bool = False


def settlement_auto_refill_idempotency_key(authorization_id: str) -> str:
    """Return the Stripe identity shared by every path for one settlement."""
    return f"auto-refill-settlement:{authorization_id}"


def maybe_charge_after_settle(
    workspace_id: str,
    *,
    settings: Settings,
    idempotency_key: str | None = None,
) -> AutoRefillOutcome:
    """Check the workspace's auto-refill state and fire a charge if due.

    Returns a structured outcome so callers can log without re-deriving
    the decision. Never raises — Stripe / network failures are surfaced
    via `last_auto_refill_status` so retries are bounded by the next
    settle event."""
    # Typed-aware: for a typed workspace the authoritative usage/reserved live in
    # the typed table; reading stale JSON here would overstate available and the
    # threshold would never trip → the card never charges (underbill).
    account = STORE.get_credit_account(workspace_id)
    if account is None:
        return AutoRefillOutcome(fired=False, reason="no_account")
    account = replace(account)  # The memory backend returns a live metadata object.
    if not account.auto_refill_enabled:
        return AutoRefillOutcome(fired=False, reason="disabled")
    if account.auto_refill_amount_microdollars <= 0:
        return AutoRefillOutcome(fired=False, reason="disabled")
    summary = live_credit_summary(workspace_id, store=STORE)
    if summary is None:
        return AutoRefillOutcome(fired=False, reason="no_account")
    # Unclamped available (may be negative for an overdrawn balance) so the
    # threshold comparison is byte-identical to the pre-C2b inline computation.
    # summary["available"] is floored at 0 for display and must NOT be used here:
    # at threshold 0 it would suppress a refill the overdraw was meant to cover.
    available = summary["total_credits"] - summary["total_usage"] - summary["reserved"]
    # The product promise is "when balance drops below the threshold".
    # Equal-to-threshold is still not below, so do not charge yet.
    if available >= account.auto_refill_threshold_microdollars:
        return AutoRefillOutcome(fired=False, reason="above_threshold")
    if not account.stripe_customer_id or not account.stripe_payment_method_id:
        return AutoRefillOutcome(fired=False, reason="no_payment_method")
    recent_attempt_reason = recent_block(account, refill_now())
    if recent_attempt_reason is not None:
        return AutoRefillOutcome(fired=False, reason=recent_attempt_reason, retryable=True)
    if not settings.stripe_secret_key:
        return AutoRefillOutcome(fired=False, reason="stripe_not_configured", retryable=True)

    # Closure archives the workspace and disables refill in the same commit.
    # Recheck here as an earlier account read may predate that commit.
    if STORE.get_workspace(workspace_id) is None:
        return AutoRefillOutcome(fired=False, reason="account_closed")

    if account.auto_refill_threshold_microdollars >= account.auto_refill_amount_microdollars:
        # Saving this is refused now (money.check_auto_refill_settings), but a
        # configuration saved before that rule would charge again after
        # every small settle. Do not charge it; the owner must fix it. Checked
        # here, where a charge would otherwise go out, so a workspace above
        # its threshold does not log on every settle.
        log.warning(
            "auto_refill.threshold_not_below_amount workspace=%s threshold=%s amount=%s",
            workspace_id,
            account.auto_refill_threshold_microdollars,
            account.auto_refill_amount_microdollars,
        )
        return AutoRefillOutcome(fired=False, reason="threshold_not_below_amount")

    try:
        import stripe
    except ImportError:  # pragma: no cover - stripe is in dependencies.
        return AutoRefillOutcome(fired=False, reason="stripe_not_installed", retryable=True)

    stripe.api_key = settings.stripe_secret_key
    credit_amount_cents = max(50, account.auto_refill_amount_microdollars // 10_000)
    fee = stripe_processing_fee(
        credit_amount_cents=credit_amount_cents,
        variable_basis_points=settings.stripe_card_fee_basis_points,
        fixed_fee_cents=settings.stripe_card_fee_fixed_cents,
        minimum_fee_cents=settings.checkout_card_fee_minimum_cents,
        fee_multiplier_basis_points=settings.stripe_paypal_fee_multiplier_basis_points,
    )
    candidate = idempotency_key or f"auto-refill:{workspace_id}:{uuid.uuid4().hex}"
    metadata = fee.metadata(
        workspace_id=workspace_id,
        payment_method="card",
        initiating_user_id=(
            workspace.owner_user_id
            if (workspace := STORE.get_workspace(workspace_id)) is not None
            else None
        ),
    )
    metadata.update(
        {
            "auto_refill": "true",
            # Preserve the configured ledger principal even if a legacy
            # account somehow contains a sub-cent value.
            "amount_microdollars": str(account.auto_refill_amount_microdollars),
        }
    )
    params: dict[str, Any] = {
        "amount": fee.charge_amount_cents, "currency": "usd", "customer": account.stripe_customer_id,
        "payment_method": account.stripe_payment_method_id,
        "payment_method_types": list(AUTO_REFILL_PAYMENT_METHOD_TYPES),
        "off_session": True, "confirm": True, "description": "TrustedRouter auto-refill",
        "amount_details": {"line_items": fee.payment_intent_line_items()}, "metadata": metadata,
    }
    payload_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    reason, key = STORE.claim_auto_refill(account, candidate=candidate, payload_hash=payload_hash)
    if key is None:
        return AutoRefillOutcome(
            False, reason, retryable=reason in {"pending", "account_changed", "rate_limited", "reconcile_required"},
        )
    metadata["refill_attempt_key"] = key
    try:
        intent = stripe.PaymentIntent.create(**params, idempotency_key=key)
    except stripe.CardError as exc:
        STORE.record_auto_refill_outcome(workspace_id, status=f"failed:{exc.code or 'card_error'}", attempt_key=key)
        log.warning("auto_refill.card_error workspace=%s code=%s", workspace_id, exc.code)
        return AutoRefillOutcome(fired=False, reason=f"stripe_error:{exc.code or 'card_error'}")
    except Exception as exc:  # noqa: BLE001 - any Stripe error blocks the refill.
        # Delivery is ambiguous. Keep the durable claim and its original Stripe key.
        log.exception("auto_refill.error workspace=%s", workspace_id)
        return AutoRefillOutcome(
            fired=False,
            reason=f"stripe_error:{type(exc).__name__}",
            retryable=True,
        )

    STORE.record_auto_refill_outcome(workspace_id, status="pending", attempt_key=key)
    return AutoRefillOutcome(fired=True, reason="charged", payment_intent_id=intent.id)
