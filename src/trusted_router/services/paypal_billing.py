from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from trusted_router.acquisition import record_checkout_started, record_credit_purchase
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    dollars_to_cents,
    dollars_to_microdollars,
    money_pair,
)
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.stripe_fees import (
    CREDITS_LINE_ITEM_NAME,
    PROCESSING_FEE_LINE_ITEM_NAME,
    ProcessingFee,
    stripe_processing_fee,
)
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

_TOKEN_CACHE_SECONDS_SKEW = 60
_TOKEN_CACHE_LOCK = threading.Lock()
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_CHECKOUT_REFERENCE_VERSION = "tr1"


@dataclass(frozen=True)
class PayPalCaptureResult:
    order_id: str
    capture_id: str
    workspace_id: str
    amount_microdollars: int
    credited: bool
    status: str
    processing_fee_microdollars: int = 0
    charge_amount_microdollars: int = 0


@dataclass(frozen=True)
class _PayPalCreditReference:
    workspace_id: str
    credit_amount_cents: int | None = None
    charge_amount_cents: int | None = None


def create_paypal_checkout_session(
    *,
    body: CheckoutRequest,
    workspace_id: str,
    customer_email: str | None,
    settings: Settings,
) -> dict[str, Any]:
    credit_amount_cents = dollars_to_cents(body.amount)
    amount_microdollars = dollars_to_microdollars(body.amount)
    fee = stripe_processing_fee(
        credit_amount_cents=credit_amount_cents,
        variable_basis_points=settings.stripe_card_fee_basis_points,
        fixed_fee_cents=settings.stripe_card_fee_fixed_cents,
    )
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None:
        raise api_error(404, "Workspace not found", ErrorType.NOT_FOUND)

    if not settings.paypal_enabled:
        if settings.environment.lower() in {"local", "test"}:
            record_checkout_started(
                workspace_id,
                amount_microdollars=amount_microdollars,
                payment_method="paypal",
            )
            return {
                "id": f"paypal_mock_{uuid.uuid4().hex}",
                "url": f"https://{settings.trusted_domain}/billing/mock-paypal-checkout",
                "workspace_id": workspace_id,
                **money_pair("amount", amount_microdollars),
                **money_pair("processing_fee", fee.processing_fee_microdollars),
                **money_pair("total", fee.charge_amount_microdollars),
                "mode": "mock_paypal",
            }
        raise api_error(400, "PayPal checkout is not configured", ErrorType.BAD_REQUEST)

    order_request_id = f"tr-paypal-order-{uuid.uuid4().hex}"
    success_url = body.success_url or f"https://{settings.trusted_domain}/billing/paypal/success"
    cancel_url = body.cancel_url or f"https://{settings.trusted_domain}/billing/paypal/cancel"
    order = _paypal_post(
        settings,
        "/v2/checkout/orders",
        request_id=order_request_id,
        json_body={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": workspace_id,
                    "custom_id": _paypal_checkout_reference(workspace_id, fee),
                    "description": "TrustedRouter prepaid credits",
                    "amount": {
                        "currency_code": "USD",
                        "value": _paypal_amount_value(fee.charge_amount_cents),
                        "breakdown": {
                            "item_total": {
                                "currency_code": "USD",
                                "value": _paypal_amount_value(fee.charge_amount_cents),
                            }
                        },
                    },
                    "items": _paypal_line_items(fee),
                }
            ],
            "application_context": {
                "brand_name": "TrustedRouter",
                "landing_page": "LOGIN",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": success_url,
                "cancel_url": cancel_url,
            },
        },
    )
    approval_url = _approval_url(order)
    order_id = str(order.get("id") or "")
    if not order_id or not approval_url:
        raise api_error(502, "PayPal did not return an approval URL", ErrorType.INTERNAL_ERROR)
    record_checkout_started(
        workspace_id,
        amount_microdollars=amount_microdollars,
        payment_method="paypal",
    )
    return {
        "id": order_id,
        "url": approval_url,
        "workspace_id": workspace_id,
        **money_pair("amount", amount_microdollars),
        **money_pair("processing_fee", fee.processing_fee_microdollars),
        **money_pair("total", fee.charge_amount_microdollars),
        "mode": "paypal",
    }


def capture_paypal_order_for_workspace(
    *,
    order_id: str,
    workspace_id: str,
    settings: Settings,
) -> PayPalCaptureResult:
    if not settings.paypal_enabled:
        raise api_error(400, "PayPal checkout is not configured", ErrorType.BAD_REQUEST)
    order = _paypal_post(
        settings,
        f"/v2/checkout/orders/{order_id}/capture",
        request_id=f"tr-paypal-capture-{order_id}",
        json_body={},
    )
    result = credit_paypal_capture(order, expected_workspace_id=workspace_id)
    if result.order_id == "":
        return PayPalCaptureResult(
            order_id=order_id,
            capture_id=result.capture_id,
            workspace_id=result.workspace_id,
            amount_microdollars=result.amount_microdollars,
            credited=result.credited,
            status=result.status,
            processing_fee_microdollars=result.processing_fee_microdollars,
            charge_amount_microdollars=result.charge_amount_microdollars,
        )
    return result


def credit_paypal_capture(
    event_or_order: Mapping[str, Any],
    *,
    expected_workspace_id: str | None = None,
) -> PayPalCaptureResult:
    parsed = _extract_capture(event_or_order)
    if parsed["status"] != "COMPLETED":
        raise api_error(400, "PayPal capture is not completed", ErrorType.BAD_REQUEST)
    workspace_id = parsed["workspace_id"]
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        raise api_error(403, "PayPal order belongs to a different workspace", ErrorType.FORBIDDEN)
    if STORE.get_credit_account(workspace_id) is None:
        raise api_error(404, "Credit account not found", ErrorType.NOT_FOUND)
    amount_microdollars = parsed["amount_microdollars"]
    processing_fee_microdollars = parsed["processing_fee_microdollars"]
    charge_amount_microdollars = parsed["charge_amount_microdollars"]
    capture_id = parsed["capture_id"]
    credited = STORE.credit_workspace_typed_direct(
        workspace_id,
        amount_microdollars,
        f"paypal_capture:{capture_id}",
    )
    if credited:
        record_credit_purchase(
            workspace_id,
            amount_microdollars=amount_microdollars,
            payment_method="paypal",
        )
    return PayPalCaptureResult(
        order_id=parsed["order_id"],
        capture_id=capture_id,
        workspace_id=workspace_id,
        amount_microdollars=amount_microdollars,
        credited=credited,
        status=parsed["status"],
        processing_fee_microdollars=processing_fee_microdollars,
        charge_amount_microdollars=charge_amount_microdollars,
    )


def verify_paypal_webhook_signature(
    *,
    headers: Mapping[str, str],
    event: Mapping[str, Any],
    settings: Settings,
) -> None:
    if not settings.paypal_enabled:
        raise api_error(400, "PayPal webhook is not configured", ErrorType.BAD_REQUEST)
    if not settings.paypal_webhook_id:
        if settings.environment.lower() == "production" and settings.paypal_enabled:
            raise api_error(400, "PayPal webhook verification is not configured", ErrorType.BAD_REQUEST)
        return
    verification = _paypal_post(
        settings,
        "/v1/notifications/verify-webhook-signature",
        request_id=f"tr-paypal-webhook-verify-{event.get('id') or uuid.uuid4().hex}",
        json_body={
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "cert_url": headers.get("paypal-cert-url"),
            "auth_algo": headers.get("paypal-auth-algo"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "webhook_id": settings.paypal_webhook_id,
            "webhook_event": dict(event),
        },
    )
    if verification.get("verification_status") != "SUCCESS":
        raise api_error(400, "Invalid PayPal webhook", ErrorType.BAD_REQUEST)


def _paypal_post(
    settings: Settings,
    path: str,
    *,
    request_id: str,
    json_body: Mapping[str, Any],
) -> dict[str, Any]:
    token = _access_token(settings)
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                f"{_paypal_base_url(settings)}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": request_id,
                    "Prefer": "return=representation",
                },
                json=dict(json_body),
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise api_error(502, "PayPal request failed", ErrorType.INTERNAL_ERROR) from exc
    if not isinstance(data, dict):
        raise api_error(502, "PayPal returned an invalid response", ErrorType.INTERNAL_ERROR)
    return data


def _access_token(settings: Settings) -> str:
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise api_error(400, "PayPal checkout is not configured", ErrorType.BAD_REQUEST)
    cache_key = (_paypal_base_url(settings), settings.paypal_client_id)
    now = time.monotonic()
    with _TOKEN_CACHE_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{_paypal_base_url(settings)}/v1/oauth2/token",
                auth=(settings.paypal_client_id, settings.paypal_client_secret),
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en_US",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            )
            response.raise_for_status()
            token_response = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise api_error(502, "PayPal authentication failed", ErrorType.INTERNAL_ERROR) from exc
    access_token = token_response.get("access_token")
    expires_in = token_response.get("expires_in")
    if not isinstance(access_token, str):
        raise api_error(502, "PayPal authentication returned no token", ErrorType.INTERNAL_ERROR)
    ttl = int(expires_in) if isinstance(expires_in, int | float | str) else 300
    expires_at = now + max(1, ttl - _TOKEN_CACHE_SECONDS_SKEW)
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE[cache_key] = (access_token, expires_at)
    return access_token


def _paypal_base_url(settings: Settings) -> str:
    return settings.paypal_api_base_url.rstrip("/")


def _paypal_amount_value(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _paypal_checkout_reference(workspace_id: str, fee: ProcessingFee) -> str:
    return "|".join(
        (
            _CHECKOUT_REFERENCE_VERSION,
            workspace_id,
            str(fee.credit_amount_cents),
            str(fee.charge_amount_cents),
        )
    )


def _paypal_line_items(fee: ProcessingFee) -> list[dict[str, Any]]:
    items = [
        {
            "name": CREDITS_LINE_ITEM_NAME,
            "unit_amount": {
                "currency_code": "USD",
                "value": _paypal_amount_value(fee.credit_amount_cents),
            },
            "quantity": "1",
            "category": "DIGITAL_GOODS",
        }
    ]
    if fee.processing_fee_cents:
        items.append(
            {
                "name": PROCESSING_FEE_LINE_ITEM_NAME,
                "unit_amount": {
                    "currency_code": "USD",
                    "value": _paypal_amount_value(fee.processing_fee_cents),
                },
                "quantity": "1",
                "category": "DIGITAL_GOODS",
            }
        )
    return items


def _approval_url(order: Mapping[str, Any]) -> str | None:
    links = order.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel") or "").lower()
        href = link.get("href")
        if rel in {"approve", "payer-action"} and isinstance(href, str):
            return href
    return None


def _extract_capture(event_or_order: Mapping[str, Any]) -> dict[str, Any]:
    if event_or_order.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        resource = event_or_order.get("resource")
        if not isinstance(resource, dict):
            raise api_error(400, "PayPal webhook has no capture resource", ErrorType.BAD_REQUEST)
        return _paypal_capture_payload(
            resource,
            order_id=_paypal_related_order_id(resource),
        )

    order_id = str(event_or_order.get("id") or "")
    purchase_units = event_or_order.get("purchase_units")
    if not isinstance(purchase_units, list) or not purchase_units:
        raise api_error(400, "PayPal order has no purchase unit", ErrorType.BAD_REQUEST)
    unit = purchase_units[0]
    if not isinstance(unit, dict):
        raise api_error(400, "PayPal order has an invalid purchase unit", ErrorType.BAD_REQUEST)
    captures = ((unit.get("payments") or {}).get("captures") if isinstance(unit.get("payments"), dict) else None)
    if not isinstance(captures, list) or not captures:
        raise api_error(400, "PayPal order has no capture", ErrorType.BAD_REQUEST)
    capture = captures[0]
    if not isinstance(capture, dict):
        raise api_error(400, "PayPal capture is invalid", ErrorType.BAD_REQUEST)
    return _paypal_capture_payload(capture, order_id=order_id, fallback=unit)


def _paypal_capture_payload(
    capture: Mapping[str, Any],
    *,
    order_id: str,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capture_id = str(capture.get("id") or "")
    if not capture_id:
        raise api_error(400, "PayPal capture has no id", ErrorType.BAD_REQUEST)
    reference = _paypal_credit_reference(capture, fallback=fallback)
    charge_amount_microdollars = _paypal_amount_microdollars(capture.get("amount"))
    amount_microdollars = charge_amount_microdollars
    processing_fee_microdollars = 0
    if reference.charge_amount_cents is not None:
        expected_charge = reference.charge_amount_cents * 10_000
        if charge_amount_microdollars != expected_charge:
            raise api_error(400, "PayPal capture amount mismatch", ErrorType.BAD_REQUEST)
        if reference.credit_amount_cents is None:
            raise api_error(400, "PayPal capture reference is invalid", ErrorType.BAD_REQUEST)
        amount_microdollars = reference.credit_amount_cents * 10_000
        processing_fee_microdollars = charge_amount_microdollars - amount_microdollars
    return {
        "order_id": order_id,
        "capture_id": capture_id,
        "workspace_id": reference.workspace_id,
        "amount_microdollars": amount_microdollars,
        "processing_fee_microdollars": processing_fee_microdollars,
        "charge_amount_microdollars": charge_amount_microdollars,
        "status": str(capture.get("status") or ""),
    }


def _paypal_credit_reference(
    resource: Mapping[str, Any],
    *,
    fallback: Mapping[str, Any] | None = None,
) -> _PayPalCreditReference:
    raw_reference = resource.get("custom_id")
    if not isinstance(raw_reference, str) and fallback is not None:
        raw_reference = fallback.get("custom_id") or fallback.get("reference_id")
    if not isinstance(raw_reference, str) or not raw_reference:
        raise api_error(400, "PayPal capture has no workspace reference", ErrorType.BAD_REQUEST)
    if not raw_reference.startswith(f"{_CHECKOUT_REFERENCE_VERSION}|"):
        return _PayPalCreditReference(workspace_id=raw_reference)
    parts = raw_reference.split("|")
    if len(parts) != 4 or not parts[1]:
        raise api_error(400, "PayPal capture reference is invalid", ErrorType.BAD_REQUEST)
    try:
        credit_amount_cents = int(parts[2])
        charge_amount_cents = int(parts[3])
    except ValueError as exc:
        raise api_error(400, "PayPal capture reference is invalid", ErrorType.BAD_REQUEST) from exc
    if credit_amount_cents <= 0 or charge_amount_cents < credit_amount_cents:
        raise api_error(400, "PayPal capture reference is invalid", ErrorType.BAD_REQUEST)
    return _PayPalCreditReference(
        workspace_id=parts[1],
        credit_amount_cents=credit_amount_cents,
        charge_amount_cents=charge_amount_cents,
    )


def _paypal_related_order_id(resource: Mapping[str, Any]) -> str:
    supplementary_data = resource.get("supplementary_data")
    if not isinstance(supplementary_data, dict):
        return ""
    related_ids = supplementary_data.get("related_ids")
    if not isinstance(related_ids, dict):
        return ""
    order_id = related_ids.get("order_id")
    return order_id if isinstance(order_id, str) else ""


def _paypal_amount_microdollars(amount: Any) -> int:
    if not isinstance(amount, dict) or amount.get("currency_code") != "USD":
        raise api_error(400, "PayPal capture currency must be USD", ErrorType.BAD_REQUEST)
    value = amount.get("value")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise api_error(400, "PayPal capture amount is invalid", ErrorType.BAD_REQUEST) from exc
    if not decimal.is_finite() or decimal <= 0:
        raise api_error(400, "PayPal capture amount is invalid", ErrorType.BAD_REQUEST)
    microdollars = int((decimal * MICRODOLLARS_PER_DOLLAR).to_integral_value())
    if microdollars % 10_000:
        raise api_error(400, "PayPal capture amount is invalid", ErrorType.BAD_REQUEST)
    return microdollars
