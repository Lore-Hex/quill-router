"""No new money into a workspace the operator latched for abuse.

A latched workspace cannot spend, so a top-up it makes is money TrustedRouter
will most likely refund, or lose to another chargeback. Checkout is refused
before any provider order or session exists (Stripe Checkout and PayPal). A
payment that still lands (an order approved before the latch, an in-flight
auto-refill) is credited so the trust ledger and any later refund stay
consistent, but it is held: the abuse pause blocks every spend path (new
authorizations, credit transfers out) until an operator clears it. An ops
alert names the payment so the operator can refund it.
"""

from __future__ import annotations

import logging
from typing import Any

from trusted_router.errors import api_error
from trusted_router.synthetic.alerts import ops_alert
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

ABUSE_PAUSE_CAUSE = "abuse"
CHECKOUT_GUARD_BILLING_PAUSED = "billing_paused"
BILLING_PAUSED_MESSAGE = (
    "Billing for this workspace is paused pending an account review, so it cannot "
    "add credits. Contact support."
)
HELD_TOPUP_FINGERPRINT = ["paused-topup", "held"]


def abuse_paused(workspace: Any) -> bool:
    return ABUSE_PAUSE_CAUSE in (getattr(workspace, "billing_pause_causes", None) or ())


def refuse_topup_if_abuse_paused(workspace: Any) -> None:
    """Raise a 403 before a checkout is created for an abuse-latched workspace."""
    if workspace is not None and abuse_paused(workspace):
        log.warning("paused_topup.refused", extra={"workspace_id": getattr(workspace, "id", "")})
        raise api_error(
            403,
            BILLING_PAUSED_MESSAGE,
            ErrorType.FORBIDDEN,
            extra={"checkout_guard": CHECKOUT_GUARD_BILLING_PAUSED},
        )


def alert_held_topup(
    store: Any,
    *,
    workspace_id: str,
    provider: str,
    payment_ref: str,
    amount_microdollars: int,
) -> bool:
    """After a credit committed: alert when it landed on an abuse-latched workspace.

    Returns whether the credit is held. Never raises; the money already moved.
    """
    try:
        if not abuse_paused(store.get_workspace(workspace_id)):
            return False
        ops_alert(
            f"paused_topup.held provider={provider} workspace={workspace_id} "
            f"payment={payment_ref} amount_usd={amount_microdollars / 1_000_000:.2f}; "
            "credit is held by the abuse pause, refund it at the provider",
            fingerprint=HELD_TOPUP_FINGERPRINT,
            tags={
                "tr_component": "paused_topup",
                "workspace_id": workspace_id,
                "payment_provider": provider,
            },
            claim_detail=f"{provider}:{payment_ref}",
        )
        return True
    except Exception:  # noqa: BLE001 - observation after a committed credit
        log.warning("paused_topup.alert_failed", exc_info=True)
        return False
