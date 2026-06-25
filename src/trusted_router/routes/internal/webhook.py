"""/internal/stripe/webhook — handles four Stripe event types:

* checkout.session.completed (payment, prepaid credit add)
* checkout.session.completed mode=setup (saved-card capture)
* setup_intent.succeeded (saved-card capture from PaymentIntent flow)
* payment_intent.succeeded (auto-refill credit add)
* payment_intent.payment_failed (auto-refill error logged)

Tests monkeypatch this module's `stripe.Webhook.construct_event` to
inject events without a real signature, so the import has to live
here, not in __init__.py.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import stripe
from fastapi import APIRouter, HTTPException, Request

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.money import MICRODOLLARS_PER_CENT
from trusted_router.routes.helpers import json_body
from trusted_router.services.x402_billing import X402_PAYMENT_METHOD, credit_x402_payment_intent
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def _grant_trial_credit_on_card_attach(
    workspace_id: str, amount_microdollars: int
) -> int:
    """First time a valid card is attached to this workspace, grant the
    configured trial credit (settings.signup_trial_credit_microdollars).
    Idempotent across webhook replays + repeat setup_intents (e.g. user adds a
    second card later) by using a deterministic per-workspace event_id —
    credit_workspace_once dedupes via the stripe_events ledger so the trial only
    ever lands once.

    Returns the amount actually credited: 0 if already granted previously, if
    the grant is disabled (amount_microdollars <= 0 — the default policy as of
    2026-06-25: NO free credit for new users), else the configured amount on
    the first attach.

    Trial credit was previously granted at signup; it then required a
    Stripe-validated card to defend against throwaway-email farming, and now
    defaults to $0. See storage.py / storage_gcp.py create_workspace for the
    matching "$0 at creation" change.
    """
    if amount_microdollars <= 0:
        return 0
    event_id = f"trial:{workspace_id}"
    if STORE.credit_workspace_once(workspace_id, amount_microdollars, event_id):
        return amount_microdollars
    return 0


def register(router: APIRouter) -> None:
    @router.post("/internal/stripe/webhook")
    async def stripe_webhook(request: Request, settings: SettingsDep) -> dict[str, Any]:
        raw = await request.body()
        sig = request.headers.get("stripe-signature")
        if settings.stripe_webhook_secret:
            try:
                constructed = stripe.Webhook.construct_event(
                    raw, sig, settings.stripe_webhook_secret
                )
            except Exception as exc:
                raise api_error(400, "Invalid Stripe webhook", ErrorType.BAD_REQUEST) from exc
            # `construct_event` returns a `stripe.Event` (a `StripeObject`
            # subclass), NOT a dict. Newer Stripe SDK versions no longer
            # expose `.get()` on StripeObject — attribute lookup raises
            # AttributeError instead. This entire handler is written
            # against dict semantics (.get with defaults, nested dicts),
            # so convert to a plain dict ONCE here and use that everywhere
            # downstream. This was the 2026-05-23 production bug behind
            # Gabriella's $5+$2 not crediting AND the post-rotation $1
            # synthetic chain-test failing — handler 500'd on the FIRST
            # `event.get("id")` call, never even reached credit_workspace_once.
            # The leading-underscore method name is unfortunate but
            # `_to_dict_recursive()` is the only walk-nested-StripeObjects
            # converter the SDK exposes; the public `to_dict()` is
            # shallow-only and would leave `data.object` as a StripeObject.
            #
            # Some unit tests monkeypatch construct_event to return a plain
            # dict directly — accept that shape too so the conversion only
            # runs when needed.
            if isinstance(constructed, dict):
                event: dict[str, Any] = constructed
            else:
                event = constructed._to_dict_recursive()  # noqa: SLF001
        else:
            event = await json_body(request)
        event_id = str(event.get("id") or uuid.uuid4())
        event_type = event.get("type")

        if event_type == "checkout.session.completed":
            obj = event.get("data", {}).get("object", {})
            workspace_id = obj.get("metadata", {}).get("workspace_id")
            amount_total = int(obj.get("amount_total") or 0)
            customer_id = obj.get("customer")
            if workspace_id and STORE.get_credit_account(workspace_id) is not None:
                if obj.get("mode") == "setup":
                    if isinstance(customer_id, str):
                        STORE.set_stripe_customer(workspace_id, customer_id=customer_id)
                    return {
                        "data": {
                            "setup_pending": True,
                            "event_id": event_id,
                            "trial_credit_granted_microdollars": 0,
                        }
                    }
                credited = STORE.credit_workspace_once(
                    workspace_id, amount_total * MICRODOLLARS_PER_CENT, event_id
                )
                # Capture the Stripe customer the first time they pay so
                # auto-refill can use it later. The default payment method
                # arrives separately in `setup_intent.succeeded` (or via the
                # PaymentIntent's `payment_method` if Checkout was set up
                # with `setup_future_usage`).
                if isinstance(customer_id, str):
                    STORE.set_stripe_customer(workspace_id, customer_id=customer_id)
                # A successful paid checkout is the strongest possible
                # card-validation signal — Stripe just successfully charged
                # the card. Grant the trial credit too if it hasn't been
                # granted yet (idempotent via the per-workspace event_id).
                granted = _grant_trial_credit_on_card_attach(
                    workspace_id, settings.signup_trial_credit_microdollars
                )
                return {
                    "data": {
                        "credited": credited,
                        "event_id": event_id,
                        "trial_credit_granted_microdollars": granted,
                    }
                }

        if event_type == "setup_intent.succeeded":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            workspace_id = metadata.get("workspace_id")
            customer_id = obj.get("customer")
            payment_method = obj.get("payment_method")
            if (
                isinstance(workspace_id, str)
                and isinstance(customer_id, str)
                and isinstance(payment_method, str)
                and STORE.get_credit_account(workspace_id) is not None
            ):
                STORE.set_stripe_customer(
                    workspace_id,
                    customer_id=customer_id,
                    payment_method_id=payment_method,
                )
                granted = _grant_trial_credit_on_card_attach(
                    workspace_id, settings.signup_trial_credit_microdollars
                )
                return {
                    "data": {
                        "setup_saved": True,
                        "event_id": event_id,
                        "trial_credit_granted_microdollars": granted,
                    }
                }

        if event_type == "payment_intent.succeeded":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            if metadata.get("payment_method") == X402_PAYMENT_METHOD:
                try:
                    result = credit_x402_payment_intent(
                        obj,
                        expected_workspace_id=None,
                        settings=settings,
                    )
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    log.error(
                        "x402.orphan_payment_intent",
                        extra={
                            "event_id": event_id,
                            "payment_intent_id": obj.get("id"),
                            "workspace_id": metadata.get("workspace_id"),
                        },
                    )
                    return {
                        "data": {
                            "event_id": event_id,
                            "x402": True,
                            "orphan": True,
                            "credited": False,
                        }
                    }
                return {"data": {"event_id": event_id, "x402": True, **result}}
            workspace_id = metadata.get("workspace_id")
            amount_microdollars_raw = metadata.get("amount_microdollars")
            if (
                metadata.get("auto_refill") == "true"
                and isinstance(workspace_id, str)
                and isinstance(amount_microdollars_raw, str)
            ):
                amount_microdollars = int(amount_microdollars_raw)
                credited = STORE.credit_workspace_once(
                    workspace_id, amount_microdollars, event_id
                )
                STORE.record_auto_refill_outcome(workspace_id, status="succeeded")
                # Also persist the payment-method if Stripe surfaced one —
                # first auto-refill after a Checkout that didn't include
                # setup_future_usage might be the first time we see the PM.
                payment_method = obj.get("payment_method")
                if isinstance(payment_method, str):
                    STORE.set_stripe_customer(
                        workspace_id,
                        customer_id=str(obj.get("customer") or ""),
                        payment_method_id=payment_method,
                    )
                return {"data": {"credited": credited, "event_id": event_id, "auto_refill": True}}
            if isinstance(workspace_id, str) and STORE.get_credit_account(workspace_id) is not None:
                payment_method = obj.get("payment_method")
                customer_id = obj.get("customer")
                if isinstance(payment_method, str) and isinstance(customer_id, str):
                    STORE.set_stripe_customer(
                        workspace_id,
                        customer_id=customer_id,
                        payment_method_id=payment_method,
                    )
                    return {
                        "data": {
                            "payment_method_saved": True,
                            "event_id": event_id,
                            "trial_credit_granted_microdollars": 0,
                        }
                }

        if event_type in {
            "payment_intent.processing",
            "payment_intent.requires_action",
            "payment_intent.canceled",
            "payment_intent.payment_failed",
        }:
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            if metadata.get("payment_method") == X402_PAYMENT_METHOD:
                return {
                    "data": {
                        "event_id": event_id,
                        "x402": True,
                        "status": obj.get("status") or event_type.removeprefix("payment_intent."),
                        "payment_intent_id": obj.get("id"),
                        "credited": False,
                    }
                }

        if event_type == "payment_intent.payment_failed":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            workspace_id = metadata.get("workspace_id")
            if metadata.get("auto_refill") == "true" and isinstance(workspace_id, str):
                last_error = obj.get("last_payment_error") or {}
                code = last_error.get("code") or "unknown"
                STORE.record_auto_refill_outcome(workspace_id, status=f"failed:{code}")
                return {"data": {"event_id": event_id, "auto_refill_failed": True, "code": code}}

        if event_type in {"charge.refunded", "charge.refund.updated"}:
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            if metadata.get("payment_method") == X402_PAYMENT_METHOD:
                log.warning(
                    "x402.refund_requires_manual_review",
                    extra={
                        "event_id": event_id,
                        "payment_intent_id": obj.get("payment_intent"),
                        "refund_id": obj.get("id"),
                    },
                )
                return {
                    "data": {
                        "event_id": event_id,
                        "x402": True,
                        "refund_requires_manual_review": True,
                        "credited": False,
                    }
                }

        return {"data": {"ignored": True, "event_id": event_id}}
