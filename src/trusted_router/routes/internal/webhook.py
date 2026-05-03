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

import uuid
from typing import Any

import stripe
from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.money import MICRODOLLARS_PER_CENT
from trusted_router.routes.helpers import json_body
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register(router: APIRouter) -> None:
    @router.post("/internal/stripe/webhook")
    async def stripe_webhook(request: Request, settings: SettingsDep) -> dict[str, Any]:
        raw = await request.body()
        sig = request.headers.get("stripe-signature")
        if settings.stripe_webhook_secret:
            try:
                event = stripe.Webhook.construct_event(raw, sig, settings.stripe_webhook_secret)
            except Exception as exc:
                raise api_error(400, "Invalid Stripe webhook", ErrorType.BAD_REQUEST) from exc
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
                    return {"data": {"setup_saved": True, "event_id": event_id}}
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
                return {"data": {"credited": credited, "event_id": event_id}}

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
                return {"data": {"setup_saved": True, "event_id": event_id}}

        if event_type == "payment_intent.succeeded":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
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

        if event_type == "payment_intent.payment_failed":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            workspace_id = metadata.get("workspace_id")
            if metadata.get("auto_refill") == "true" and isinstance(workspace_id, str):
                last_error = obj.get("last_payment_error") or {}
                code = last_error.get("code") or "unknown"
                STORE.record_auto_refill_outcome(workspace_id, status=f"failed:{code}")
                return {"data": {"event_id": event_id, "auto_refill_failed": True, "code": code}}

        return {"data": {"ignored": True, "event_id": event_id}}
