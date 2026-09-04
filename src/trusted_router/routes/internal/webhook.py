"""/internal/stripe/webhook handles Stripe billing events:

* checkout.session.completed (immediate payment or delayed-payment pending state)
* checkout.session.async_payment_succeeded (delayed prepaid credit add)
* checkout.session.async_payment_failed (delayed payment failure)
* checkout.session.completed mode=setup (saved-card capture)
* setup_intent.succeeded (saved-card capture from PaymentIntent flow)
* payment_intent.succeeded (auto-refill credit add)
* payment_intent.payment_failed (auto-refill error logged)

Tests monkeypatch this module's `stripe.Webhook.construct_event` to
inject events without a real signature, so the import has to live
here, not in __init__.py.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import stripe
from fastapi import APIRouter, HTTPException, Request

from trusted_router.acquisition import (
    record_credit_purchase,
    record_payment_method_saved,
)
from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.money import MICRODOLLARS_PER_CENT
from trusted_router.routes.helpers import json_body
from trusted_router.services.x402_billing import X402_PAYMENT_METHOD, credit_x402_payment_intent
from trusted_router.storage import STORE
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)


def register(router: APIRouter) -> None:
    @router.post("/internal/stripe/webhook")
    async def stripe_webhook(request: Request, settings: SettingsDep) -> dict[str, Any]:
        raw = await request.body()
        sig = request.headers.get("stripe-signature")
        if settings.stripe_webhook_secret:
            # Reject a missing header instead of handing None to signature
            # verification. stripe-python's types now say so out loud -- the
            # parameter is `str`, not `str | None` -- but the guard is the point
            # rather than the annotation: an unsigned request must not reach
            # construct_event at all.
            if sig is None:
                raise api_error(
                    400, "Missing Stripe signature", ErrorType.BAD_REQUEST
                )
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
        elif settings.environment.lower() in {"local", "test"}:
            event = await json_body(request)
        else:
            raise api_error(
                503,
                "Stripe webhook verification is not configured",
                ErrorType.SERVICE_UNAVAILABLE,
            )
        event_id = str(event.get("id") or uuid.uuid4())
        event_type = event.get("type")

        adverse_events = _stripe_adverse_events(event, event_id=event_id)
        if adverse_events:
            results = []
            for adverse in adverse_events:
                adverse_result = STORE.record_adverse_trust_event(adverse)
                results.append(
                    {
                        "adverse_ref": adverse.adverse_ref,
                        "provider": adverse_result.provider or adverse.provider,
                        "kind": adverse.kind,
                        "status": adverse.lifecycle_status,
                        "outcome": adverse_result.outcome,
                        "workspace_id": adverse_result.workspace_id,
                        "recovery_target": adverse_result.recovery_target,
                        "unrecovered_micro": adverse_result.unrecovered_micro,
                    }
                )
            return {"data": {"event_id": event_id, "adverse": results}}

        if event_type in {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
        }:
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            workspace_id = metadata.get("workspace_id")
            amount_total = int(obj.get("amount_total") or 0)
            customer_id = obj.get("customer")
            if workspace_id and STORE.get_credit_account(workspace_id) is not None:
                if event_type == "checkout.session.completed" and obj.get("mode") == "setup":
                    if isinstance(customer_id, str):
                        STORE.set_stripe_customer(workspace_id, customer_id=customer_id)
                    return {
                        "data": {
                            "setup_pending": True,
                            "event_id": event_id,
                            "trial_credit_granted_microdollars": 0,
                        }
                    }
                if event_type == "checkout.session.async_payment_failed":
                    return {
                        "data": {
                            "credited": False,
                            "payment_failed": True,
                            "event_id": event_id,
                            "trial_credit_granted_microdollars": 0,
                        }
                    }
                if (
                    event_type == "checkout.session.completed"
                    and obj.get("payment_status") != "paid"
                ):
                    return {
                        "data": {
                            "credited": False,
                            "payment_pending": True,
                            "payment_status": obj.get("payment_status") or "unpaid",
                            "event_id": event_id,
                            "trial_credit_granted_microdollars": 0,
                        }
                    }
                amount_microdollars = _checkout_credit_amount_microdollars(
                    metadata=metadata,
                    amount_total_cents=amount_total,
                )
                payment_method = str(metadata.get("payment_method") or "stripe")
                credit_event_id = _checkout_credit_event_id(
                    event_id=event_id,
                    checkout_session=obj,
                    payment_method=payment_method,
                )
                credited = STORE.credit_workspace_typed_direct(
                    workspace_id,
                    amount_microdollars,
                    credit_event_id,
                    provenance=CreditProvenance(
                        source="checkout",
                        provider="stripe",
                        external_ref=str(obj.get("payment_intent") or ""),
                        occurred_at=datetime.fromtimestamp(
                            int(obj.get("created") or event.get("created") or 0), tz=UTC
                        ),
                    ),
                    payment_amount_microdollars=(
                        amount_total * MICRODOLLARS_PER_CENT
                    ),
                    currency=str(obj.get("currency") or "usd"),
                    lifetime_topup_user_id=_lifetime_topup_user_id(workspace_id, metadata),
                )
                if credited:
                    record_credit_purchase(
                        workspace_id,
                        amount_microdollars=amount_microdollars,
                        payment_method=(
                            "stripe_ach" if payment_method == "ach" else "stripe"
                        ),
                    )
                # Capture the Stripe customer the first time they pay so
                # auto-refill can use it later. The default payment method
                # arrives separately in `setup_intent.succeeded` (or via the
                # PaymentIntent's `payment_method` if Checkout was set up
                # with `setup_future_usage`).
                if isinstance(customer_id, str) and payment_method != "ach":
                    STORE.set_stripe_customer(workspace_id, customer_id=customer_id)
                return {
                    "data": {
                        "credited": credited,
                        "credited_microdollars": amount_microdollars,
                        "event_id": event_id,
                        # Kept for response compatibility. Starter credit is
                        # granted atomically at account creation, never here.
                        "trial_credit_granted_microdollars": 0,
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
                record_payment_method_saved(
                    workspace_id,
                    payment_method="stripe_card",
                )
                return {
                    "data": {
                        "setup_saved": True,
                        "event_id": event_id,
                        "trial_credit_granted_microdollars": 0,
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
                amount_microdollars = _auto_refill_credit_amount_microdollars(
                    metadata=metadata,
                    payment_intent_amount_cents=obj.get("amount"),
                )
                credited = STORE.credit_workspace_typed_direct(
                    workspace_id,
                    amount_microdollars,
                    event_id,
                    provenance=CreditProvenance(
                        source="auto_refill",
                        provider="stripe",
                        external_ref=str(obj.get("id") or ""),
                        occurred_at=datetime.fromtimestamp(
                            int(obj.get("created") or event.get("created") or 0), tz=UTC
                        ),
                    ),
                    payment_amount_microdollars=(
                        int(obj.get("amount") or amount_microdollars // MICRODOLLARS_PER_CENT)
                        * MICRODOLLARS_PER_CENT
                    ),
                    currency=str(obj.get("currency") or "usd"),
                    lifetime_topup_user_id=_lifetime_topup_user_id(workspace_id, metadata),
                )
                if credited:
                    record_credit_purchase(
                        workspace_id,
                        amount_microdollars=amount_microdollars,
                        payment_method="stripe_auto_refill",
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
                    record_payment_method_saved(
                        workspace_id,
                        payment_method="stripe_card",
                    )
                return {"data": {"credited": credited, "event_id": event_id, "auto_refill": True}}
            if (
                metadata.get("payment_method") in {None, "auto", "card"}
                and isinstance(workspace_id, str)
                and STORE.get_credit_account(workspace_id) is not None
            ):
                payment_method = obj.get("payment_method")
                customer_id = obj.get("customer")
                if isinstance(payment_method, str) and isinstance(customer_id, str):
                    STORE.set_stripe_customer(
                        workspace_id,
                        customer_id=customer_id,
                        payment_method_id=payment_method,
                    )
                    record_payment_method_saved(
                        workspace_id,
                        payment_method="stripe_card",
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

        # A charge.refunded delivery without any refund objects cannot provide
        # the per-refund dedup key required for order-independent partials.
        # Preserve the existing visible fallback instead of inventing one from
        # the charge id; canonical Stripe deliveries are handled above.
        if event_type == "charge.refunded":
            obj = event.get("data", {}).get("object", {})
            metadata = obj.get("metadata") or {}
            if metadata.get("payment_method") == X402_PAYMENT_METHOD:
                log.warning(
                    "x402.refund_requires_manual_review",
                    extra={
                        "event_id": event_id,
                        "payment_intent_id": obj.get("payment_intent"),
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


def _object_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return ""


def _stripe_adverse_events(
    event: dict[str, Any], *, event_id: str
) -> tuple[AdverseTrustEvent, ...]:
    event_type = str(event.get("type") or "")
    obj = event.get("data", {}).get("object", {})
    if not isinstance(obj, dict):
        return ()
    created = int(event.get("created") or obj.get("created") or 0)
    occurred_at = datetime.fromtimestamp(created, tz=UTC)
    watermark = f"{created:020d}:{event_id}"
    raw_payload = json.dumps(event, separators=(",", ":"), sort_keys=True, default=str)

    refund_objects: list[dict[str, Any]] = []
    if event_type == "charge.refunded":
        refunds = obj.get("refunds") or {}
        data = refunds.get("data") if isinstance(refunds, dict) else None
        if isinstance(data, list):
            refund_objects = [row for row in data if isinstance(row, dict)]
    elif event_type in {
        "charge.refund.updated",
        "refund.created",
        "refund.updated",
        "refund.failed",
    }:
        refund_objects = [obj]
    if refund_objects:
        parsed: list[AdverseTrustEvent] = []
        for refund in refund_objects:
            adverse_ref = str(refund.get("id") or "")
            payment_intent = _object_id(
                refund.get("payment_intent") or obj.get("payment_intent")
            )
            if not adverse_ref or not payment_intent:
                continue
            metadata = refund.get("metadata") or obj.get("metadata") or {}
            provider = (
                "x402"
                if isinstance(metadata, dict)
                and metadata.get("payment_method") == X402_PAYMENT_METHOD
                else "stripe"
            )
            raw_status = str(refund.get("status") or "succeeded").lower()
            status = {
                "pending": "pending",
                "requires_action": "pending",
                "succeeded": "succeeded",
                "failed": "failed",
                "canceled": "failed",
                "cancelled": "failed",
                "reversed": "reversed",
            }.get(raw_status, "pending")
            parsed.append(
                AdverseTrustEvent(
                    event_id=f"{event_id}:{adverse_ref}",
                    provider=provider,
                    kind="refund",
                    adverse_ref=adverse_ref,
                    original_payment_ref=payment_intent,
                    amount_micro=int(refund.get("amount") or 0)
                    * MICRODOLLARS_PER_CENT,
                    provider_subtype=event_type,
                    lifecycle_status=status,
                    occurred_at=occurred_at,
                    provider_ordering_watermark=watermark,
                    payload=raw_payload,
                )
            )
        return tuple(parsed)

    if event_type in {
        "charge.dispute.created",
        "charge.dispute.updated",
        "charge.dispute.closed",
        "charge.dispute.funds_withdrawn",
        "charge.dispute.funds_reinstated",
    }:
        adverse_ref = str(obj.get("id") or "")
        charge = obj.get("charge") or {}
        payment_intent = _object_id(
            obj.get("payment_intent")
            or (charge.get("payment_intent") if isinstance(charge, dict) else None)
        )
        if not adverse_ref or not payment_intent:
            return ()
        metadata = obj.get("metadata") or (
            charge.get("metadata") if isinstance(charge, dict) else {}
        )
        provider = (
            "x402"
            if isinstance(metadata, dict)
            and metadata.get("payment_method") == X402_PAYMENT_METHOD
            else "stripe"
        )
        raw_status = str(obj.get("status") or "").lower()
        if event_type == "charge.dispute.funds_reinstated" or raw_status == "won":
            status = "won"
        elif raw_status == "lost":
            status = "lost"
        elif raw_status in {"closed", "warning_closed"}:
            status = "closed"
        elif raw_status in {"warning_needs_response", "warning_under_review"}:
            status = "pending"
        else:
            # A real dispute removes the funds immediately, before Stripe has
            # adjudicated it; that active state claims all credited principal.
            status = "succeeded"
        return (
            AdverseTrustEvent(
                event_id=f"{event_id}:{adverse_ref}",
                provider=provider,
                kind="dispute",
                adverse_ref=adverse_ref,
                original_payment_ref=payment_intent,
                amount_micro=int(obj.get("amount") or 0) * MICRODOLLARS_PER_CENT,
                provider_subtype=event_type,
                lifecycle_status=status,
                occurred_at=occurred_at,
                provider_ordering_watermark=watermark,
                payload=raw_payload,
            ),
        )
    return ()


def _lifetime_topup_user_id(workspace_id: str, metadata: dict[str, Any]) -> str | None:
    initiating_user_id = metadata.get("initiating_user_id")
    if isinstance(initiating_user_id, str) and initiating_user_id:
        return initiating_user_id
    workspace = STORE.get_workspace(workspace_id)
    return workspace.owner_user_id if workspace is not None else None


def _checkout_credit_event_id(
    *,
    event_id: str,
    checkout_session: dict[str, Any],
    payment_method: str,
) -> str:
    """Deduplicate ACH fulfillment across completed and async events.

    Card events historically used the Stripe event id, so that key remains
    unchanged. ACH is new and can safely key fulfillment to the PaymentIntent
    or Checkout Session instead.
    """
    if payment_method != "ach":
        return event_id
    payment_id = checkout_session.get("payment_intent") or checkout_session.get("id")
    if isinstance(payment_id, str) and payment_id:
        return f"stripe_checkout:{payment_id}"
    return event_id


def _checkout_credit_amount_microdollars(
    *,
    metadata: dict[str, Any],
    amount_total_cents: int,
) -> int:
    """Return credit principal, excluding the separately charged fee.

    Sessions created before fee pass-through have no principal metadata and
    retain the legacy amount_total behavior. New sessions fail closed if the
    signed Stripe event contains malformed or impossible principal metadata.
    """
    raw = metadata.get("credit_amount_microdollars")
    if raw is None:
        return amount_total_cents * MICRODOLLARS_PER_CENT
    processing_fee_raw = metadata.get("processing_fee_cents")
    charge_amount_raw = metadata.get("charge_amount_cents")
    if (
        not isinstance(raw, str)
        or not isinstance(processing_fee_raw, str)
        or not isinstance(charge_amount_raw, str)
    ):
        raise api_error(400, "Invalid Stripe credit amount", ErrorType.BAD_REQUEST)
    try:
        amount_microdollars = int(raw)
        processing_fee_cents = int(processing_fee_raw)
        charge_amount_cents = int(charge_amount_raw)
    except ValueError as exc:
        raise api_error(400, "Invalid Stripe credit amount", ErrorType.BAD_REQUEST) from exc
    if (
        amount_microdollars <= 0
        or amount_microdollars % MICRODOLLARS_PER_CENT
        or processing_fee_cents < 0
        or charge_amount_cents != amount_total_cents
        or amount_microdollars // MICRODOLLARS_PER_CENT + processing_fee_cents
        != amount_total_cents
    ):
        raise api_error(400, "Invalid Stripe credit amount", ErrorType.BAD_REQUEST)
    return amount_microdollars


def _auto_refill_credit_amount_microdollars(
    *,
    metadata: dict[str, Any],
    payment_intent_amount_cents: Any,
) -> int:
    raw = metadata.get("amount_microdollars")
    if not isinstance(raw, str):
        raise api_error(400, "Invalid Stripe refill amount", ErrorType.BAD_REQUEST)
    try:
        amount_microdollars = int(raw)
    except ValueError as exc:
        raise api_error(400, "Invalid Stripe refill amount", ErrorType.BAD_REQUEST) from exc
    if amount_microdollars <= 0 or amount_microdollars % MICRODOLLARS_PER_CENT:
        raise api_error(400, "Invalid Stripe refill amount", ErrorType.BAD_REQUEST)

    charge_amount_raw = metadata.get("charge_amount_cents")
    if charge_amount_raw is None:
        # Legacy PaymentIntents predate explicit fee metadata.
        return amount_microdollars
    processing_fee_raw = metadata.get("processing_fee_cents")
    credit_amount_raw = metadata.get("credit_amount_microdollars")
    if (
        not isinstance(charge_amount_raw, str)
        or not isinstance(processing_fee_raw, str)
        or not isinstance(credit_amount_raw, str)
        or not isinstance(payment_intent_amount_cents, int)
    ):
        raise api_error(400, "Invalid Stripe refill amount", ErrorType.BAD_REQUEST)
    try:
        charge_amount_cents = int(charge_amount_raw)
        processing_fee_cents = int(processing_fee_raw)
        credit_amount_microdollars = int(credit_amount_raw)
    except ValueError as exc:
        raise api_error(400, "Invalid Stripe refill amount", ErrorType.BAD_REQUEST) from exc
    if (
        processing_fee_cents < 0
        or credit_amount_microdollars != amount_microdollars
        or charge_amount_cents != payment_intent_amount_cents
        or amount_microdollars // MICRODOLLARS_PER_CENT + processing_fee_cents
        != charge_amount_cents
    ):
        raise api_error(400, "Invalid Stripe refill amount", ErrorType.BAD_REQUEST)
    return amount_microdollars
