from __future__ import annotations

import uuid
from typing import Any, cast

import stripe

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import dollars_to_cents, money_pair
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.stripe_fees import stripe_processing_fee
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def create_checkout_session(
    *,
    body: CheckoutRequest,
    workspace_id: str,
    customer_email: str | None,
    customer_id: str | None,
    settings: Settings,
) -> dict[str, Any]:
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None:
        raise api_error(404, "Workspace not found", ErrorType.NOT_FOUND)

    success_url = body.success_url or f"https://{settings.trusted_domain}/billing/success"
    cancel_url = body.cancel_url or f"https://{settings.trusted_domain}/billing"
    amount_cents = dollars_to_cents(body.amount)
    amount_microdollars = amount_cents * 10_000
    stablecoin_requested = body.payment_method in {"stablecoin", "crypto", "usdc"}
    ach_requested = body.payment_method in {"ach", "bank", "us_bank_account"}
    if stablecoin_requested and not settings.stablecoin_checkout_enabled:
        raise api_error(400, "Stablecoin checkout is not enabled", ErrorType.BAD_REQUEST)
    if ach_requested:
        fee_basis_points = settings.stripe_ach_fee_basis_points
        fee_fixed_cents = settings.stripe_ach_fee_fixed_cents
        fee_max_cents: int | None = settings.stripe_ach_fee_max_cents
        payment_method = "ach"
    elif stablecoin_requested:
        fee_basis_points = settings.stripe_stablecoin_fee_basis_points
        fee_fixed_cents = settings.stripe_stablecoin_fee_fixed_cents
        fee_max_cents = None
        payment_method = "stablecoin"
    else:
        fee_basis_points = settings.stripe_card_fee_basis_points
        fee_fixed_cents = settings.stripe_card_fee_fixed_cents
        fee_max_cents = None
        payment_method = "card"
    fee = stripe_processing_fee(
        credit_amount_cents=amount_cents,
        variable_basis_points=fee_basis_points,
        fixed_fee_cents=fee_fixed_cents,
        max_fee_cents=fee_max_cents,
    )
    payment_metadata = fee.metadata(
        workspace_id=workspace_id,
        payment_method=payment_method,
    )
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        session_args: dict[str, Any] = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items": fee.checkout_line_items(),
            "metadata": payment_metadata,
        }
        if stablecoin_requested:
            session_args["payment_method_types"] = ["crypto"]
            session_args["payment_intent_data"] = {"metadata": payment_metadata}
            if customer_email:
                session_args["customer_email"] = customer_email
        else:
            if customer_id:
                session_args["customer"] = customer_id
            else:
                session_args["customer_creation"] = "always"
                if customer_email:
                    session_args["customer_email"] = customer_email
            if ach_requested:
                session_args["payment_method_types"] = ["us_bank_account"]
                session_args["payment_method_options"] = {
                    "us_bank_account": {"verification_method": "automatic"}
                }
                # ACH is a one-time top-up. Saved-card auto refill remains
                # card-only because bank debits settle asynchronously.
                session_args["payment_intent_data"] = {"metadata": payment_metadata}
            else:
                session_args["payment_intent_data"] = {
                    "setup_future_usage": "off_session",
                    "metadata": payment_metadata,
                }
        session = stripe.checkout.Session.create(**session_args)
        mode = (
            "stripe_stablecoin"
            if stablecoin_requested
            else "stripe_ach"
            if ach_requested
            else "stripe"
        )
        return {
            "id": session["id"],
            "url": session["url"],
            "workspace_id": workspace_id,
            **money_pair("amount", amount_microdollars),
            **money_pair("processing_fee", fee.processing_fee_microdollars),
            **money_pair("total", fee.charge_amount_microdollars),
            "mode": mode,
        }

    mock_mode = (
        "mock_stablecoin"
        if stablecoin_requested
        else "mock_ach"
        if ach_requested
        else "mock"
    )
    return {
        "id": f"cs_test_{uuid.uuid4().hex}",
        "url": f"https://{settings.trusted_domain}/billing/mock-checkout",
        "workspace_id": workspace_id,
        **money_pair("amount", amount_microdollars),
        **money_pair("processing_fee", fee.processing_fee_microdollars),
        **money_pair("total", fee.charge_amount_microdollars),
        "mode": mock_mode,
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
        checkout_customer_id = customer_id
        if checkout_customer_id is None:
            customer_args: dict[str, Any] = {
                "metadata": {
                    "workspace_id": workspace_id,
                    "purpose": "payment_method_setup",
                }
            }
            if customer_email:
                customer_args["email"] = customer_email
            customer = stripe.Customer.create(**customer_args)
            checkout_customer_id = str(customer["id"])
        session_args: dict[str, Any] = {
            "mode": "setup",
            "payment_method_types": ["card"],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"workspace_id": workspace_id, "purpose": "payment_method_setup"},
            "setup_intent_data": {
                "metadata": {"workspace_id": workspace_id, "purpose": "payment_method_setup"}
            },
            "customer": checkout_customer_id,
        }
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


def describe_saved_payment_method(
    *,
    payment_method_id: str | None,
    settings: Settings,
) -> dict[str, Any] | None:
    if not payment_method_id:
        return None
    fallback = {
        "id_tail": payment_method_id[-6:],
        "brand": None,
        "last4": None,
        "exp_month": None,
        "exp_year": None,
        "details_available": False,
    }
    if not settings.stripe_secret_key:
        if payment_method_id.startswith("pm_mock_"):
            return {
                **fallback,
                "brand": "test",
                "last4": payment_method_id[-4:],
            }
        return fallback
    stripe.api_key = settings.stripe_secret_key
    try:
        payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
    except Exception:
        return fallback

    data = (
        cast(dict[str, Any], payment_method)
        if isinstance(payment_method, dict)
        else cast(Any, payment_method)._to_dict_recursive()
    )
    card = data.get("card") if data.get("type") == "card" else None
    if not isinstance(card, dict):
        return fallback
    return {
        **fallback,
        "brand": card.get("brand"),
        "last4": card.get("last4"),
        "exp_month": card.get("exp_month"),
        "exp_year": card.get("exp_year"),
        "details_available": True,
    }


def remove_saved_payment_method(
    *,
    workspace_id: str,
    settings: Settings,
) -> dict[str, Any]:
    account = STORE.get_credit_account(workspace_id)
    if account is None:
        raise api_error(404, "Credit account not found", ErrorType.NOT_FOUND)
    payment_method_id = account.stripe_payment_method_id
    if not payment_method_id:
        return {"removed": False, "reason": "no_payment_method"}

    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        try:
            stripe.PaymentMethod.detach(payment_method_id)
        except Exception as exc:
            raise api_error(
                503,
                "Could not remove Stripe payment method",
                ErrorType.SERVICE_UNAVAILABLE,
            ) from exc

    STORE.clear_stripe_payment_method(workspace_id)
    return {"removed": True, "payment_method_id_tail": payment_method_id[-6:]}


def list_workspace_payments(
    *,
    workspace_id: str,
    settings: Settings,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the recent Stripe payments for this workspace.

    Source of truth for "what did the user actually pay" is Stripe, not
    TR — we don't track per-payment metadata in tr_entities today
    (`stripe_event` rows only store `{"created_at": ...}` for
    idempotency dedup). So this function pulls live from Stripe's
    PaymentIntent Search API filtered by `metadata['workspace_id']:'...'`.

    Why PaymentIntent search (not checkout.Session): Stripe's Search API
    is only enabled on certain resources — PaymentIntent yes, Charge yes,
    checkout.Session NO. Our `create_checkout_session()` stamps
    `payment_intent_data.metadata.workspace_id` on every Stripe
    checkout, so the resulting PaymentIntent IS searchable by workspace.

    Returns up to `limit` rows newest-first, each with the shape:
        {
          "payment_intent": "pi_...",
          "created_at": <unix ts>,
          "amount_cents": int,
          "credit_amount_cents": int | None,
          "processing_fee_cents": int | None,
          "currency": "usd",
          "status": "succeeded" | "processing" | "requires_payment_method" | ...,
          "receipt_url": str | None,
          "payment_method_type": "card" | "ach" | "stablecoin" | None,
          "card_brand": "visa" | "mastercard" | ... | None,
          "card_last4": "4242" | None,
          "bank_name": str | None,
          "bank_last4": "6789" | None,
        }

    Failures (Stripe API down, missing key) return an empty list rather
    than raising — the credits page falls back to "no payment history
    yet" copy, which is the right UX in both legitimate-empty and
    transient-failure cases. The page still renders the balance section
    so the page isn't blocked on Stripe API uptime.
    """
    if not settings.stripe_secret_key:
        return []
    stripe.api_key = settings.stripe_secret_key
    try:
        # Search syntax: `metadata['workspace_id']:'<value>'` — single
        # quotes around the UUID are required because dashes would
        # otherwise be lexed as operator tokens. `expand` pulls the
        # latest_charge inline so we don't need a second round-trip
        # just to get card details and the hosted receipt URL.
        results = stripe.PaymentIntent.search(
            query=f"metadata['workspace_id']:'{workspace_id}'",
            limit=min(limit, 100),
            expand=["data.latest_charge"],
        )
    except Exception:
        # Stripe down, search quota exceeded, or the workspace has
        # never paid yet — all collapse to "no payments to show right
        # now." Page still renders the rest of credits view.
        return []

    out: list[dict[str, Any]] = []
    for pi in results.data[:limit]:
        pi_data = (
            cast(dict[str, Any], pi)
            if isinstance(pi, dict)
            else cast(Any, pi)._to_dict_recursive()
        )
        charge = pi_data.get("latest_charge")
        if isinstance(charge, str):  # not expanded for some reason
            charge = None
        card_brand: str | None = None
        card_last4: str | None = None
        bank_name: str | None = None
        bank_last4: str | None = None
        receipt_url: str | None = None
        if isinstance(charge, dict):
            receipt_url = charge.get("receipt_url")
            pmd = charge.get("payment_method_details") or {}
            card = pmd.get("card") if isinstance(pmd, dict) else None
            bank = pmd.get("us_bank_account") if isinstance(pmd, dict) else None
            if isinstance(card, dict):
                card_brand = card.get("brand")
                card_last4 = card.get("last4")
            if isinstance(bank, dict):
                bank_name = bank.get("bank_name")
                bank_last4 = bank.get("last4")
        metadata = pi_data.get("metadata")
        # PaymentIntent.amount is in cents already.
        out.append({
            "payment_intent": pi_data.get("id"),
            "created_at": pi_data.get("created"),
            "amount_cents": int(pi_data.get("amount") or 0),
            "credit_amount_cents": _metadata_microdollars_to_cents(
                metadata,
                key="credit_amount_microdollars",
            ),
            "processing_fee_cents": _metadata_int(
                metadata,
                key="processing_fee_cents",
            ),
            "currency": pi_data.get("currency") or "usd",
            "status": pi_data.get("status"),
            # Display-shaped synonym so the template doesn't need to know
            # the Stripe state machine — "succeeded" → "paid" reads
            # natural alongside checkout-session-style status values.
            "payment_status": (
                "paid" if pi_data.get("status") == "succeeded" else pi_data.get("status")
            ),
            "receipt_url": receipt_url,
            "payment_method_type": (
                metadata.get("payment_method") if isinstance(metadata, dict) else None
            ),
            "card_brand": card_brand,
            "card_last4": card_last4,
            "bank_name": bank_name,
            "bank_last4": bank_last4,
        })
    return out


def _metadata_int(metadata: Any, *, key: str) -> int | None:
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get(key)
    if not isinstance(raw, str):
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _metadata_microdollars_to_cents(metadata: Any, *, key: str) -> int | None:
    value = _metadata_int(metadata, key=key)
    if value is None or value % 10_000:
        return None
    return value // 10_000
