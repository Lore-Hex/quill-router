from __future__ import annotations

import uuid
from typing import Any

import stripe

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import dollars_to_cents, dollars_to_microdollars, money_pair
from trusted_router.schemas import CheckoutRequest
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def create_checkout_session(
    *,
    body: CheckoutRequest,
    workspace_id: str,
    customer_email: str | None,
    settings: Settings,
) -> dict[str, Any]:
    amount_microdollars = dollars_to_microdollars(body.amount)
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None:
        raise api_error(404, "Workspace not found", ErrorType.NOT_FOUND)

    success_url = body.success_url or f"https://{settings.trusted_domain}/billing/success"
    cancel_url = body.cancel_url or f"https://{settings.trusted_domain}/billing"
    amount_cents = dollars_to_cents(body.amount)
    stablecoin_requested = body.payment_method in {"stablecoin", "crypto", "usdc"}
    if stablecoin_requested and not settings.stablecoin_checkout_enabled:
        raise api_error(400, "Stablecoin checkout is not enabled", ErrorType.BAD_REQUEST)
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        session_args: dict[str, Any] = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items": [
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": "TrustedRouter credits"},
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            "metadata": {
                "workspace_id": workspace_id,
                "payment_method": "stablecoin" if stablecoin_requested else "auto",
            },
        }
        if stablecoin_requested:
            session_args["payment_method_types"] = ["crypto"]
            if customer_email:
                session_args["customer_email"] = customer_email
        else:
            # Capture the customer + payment method on the card path so
            # auto-refill can charge off-session later. Crypto checkouts
            # don't support `setup_future_usage`, so we only set this on
            # the card path.
            session_args["customer_creation"] = "always"
            session_args["payment_intent_data"] = {
                "setup_future_usage": "off_session",
                "metadata": {"workspace_id": workspace_id},
            }
        session = stripe.checkout.Session.create(**session_args)
        return {
            "id": session["id"],
            "url": session["url"],
            "workspace_id": workspace_id,
            **money_pair("amount", amount_microdollars),
            "mode": "stripe_stablecoin" if stablecoin_requested else "stripe",
        }

    return {
        "id": f"cs_test_{uuid.uuid4().hex}",
        "url": f"https://{settings.trusted_domain}/billing/mock-checkout",
        "workspace_id": workspace_id,
        **money_pair("amount", amount_microdollars),
        "mode": "mock_stablecoin" if stablecoin_requested else "mock",
    }


def create_payment_method_session(
    *,
    workspace_id: str,
    customer_email: str | None,
    customer_id: str | None,
    success_url: str,
    cancel_url: str,
    settings: Settings,
) -> dict[str, Any]:
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None:
        raise api_error(404, "Workspace not found", ErrorType.NOT_FOUND)

    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        session_args: dict[str, Any] = {
            "mode": "setup",
            "payment_method_types": ["card"],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"workspace_id": workspace_id, "purpose": "payment_method_setup"},
            "setup_intent_data": {
                "metadata": {"workspace_id": workspace_id, "purpose": "payment_method_setup"}
            },
        }
        if customer_id:
            session_args["customer"] = customer_id
        elif customer_email:
            session_args["customer_email"] = customer_email
        session = stripe.checkout.Session.create(**session_args)
        return {
            "id": session["id"],
            "url": session["url"],
            "workspace_id": workspace_id,
            "mode": "stripe_setup",
        }

    mock_customer_id = customer_id or f"cus_mock_{uuid.uuid4().hex}"
    mock_payment_method_id = f"pm_mock_{uuid.uuid4().hex}"
    STORE.set_stripe_customer(
        workspace_id,
        customer_id=mock_customer_id,
        payment_method_id=mock_payment_method_id,
    )
    return {
        "id": f"cs_setup_mock_{uuid.uuid4().hex}",
        "url": f"https://{settings.trusted_domain}/billing/mock-payment-method",
        "workspace_id": workspace_id,
        "mode": "mock_setup",
    }


def create_billing_portal_session(
    *,
    customer_id: str | None,
    return_url: str,
    settings: Settings,
) -> dict[str, str]:
    if settings.stripe_secret_key:
        if not customer_id:
            raise api_error(400, "No saved Stripe customer", ErrorType.BAD_REQUEST)
        stripe.api_key = settings.stripe_secret_key
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        return {"url": session["url"], "mode": "stripe"}
    return {"url": f"https://{settings.trusted_domain}/billing/mock-portal", "mode": "mock"}
