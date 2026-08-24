"""Adyen Checkout Sessions and HMAC-verified prepaid credit fulfillment.

Adyen is only a payment adapter. The typed TrustedRouter credit ledger remains
the balance authority, and a successful verified AUTHORISATION webhook is the
only operation in this module that can add credits.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from trusted_router.acquisition import record_credit_purchase
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import dollars_to_cents, money_pair
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.stripe_fees import ProcessingFee, processing_fee
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

ADYEN_WEB_JS_SRI = "sha384-B290qKFISOSRqrQlUy+IqPzFioaHcUn49xyVUlpEIqcSAt2+4bTkH0FOsICZ86o9"
ADYEN_WEB_CSS_SRI = "sha384-QCE3u3lliHH3SEx30M8NEuDhvEGmFCtZl62YVSVWw84cYNt2NYYbVwd/KPaaaIQ9"

_CHECKOUT_REFERENCE_RE = re.compile(
    r"^(?P<unsigned>trc_(?P<workspace>[0-9a-f]{32})_"
    r"(?P<credit>[1-9a-z][0-9a-z]{0,7})_"
    r"(?P<charge>[1-9a-z][0-9a-z]{0,7})_(?P<nonce>[0-9a-f]{8}))_"
    r"(?P<signature>[0-9a-f]{16})$"
)
_ADVERSE_EVENT_CODES = frozenset(
    {
        "CANCEL_OR_REFUND",
        "CANCELLATION",
        "CAPTURE_FAILED",
        "CHARGEBACK",
        "NOTIFICATION_OF_CHARGEBACK",
        "NOTIFICATION_OF_FRAUD",
        "REFUND",
        "REFUND_FAILED",
        "REFUNDED_REVERSED",
        "SECOND_CHARGEBACK",
        "TECHNICAL_CANCEL",
    }
)


@dataclass(frozen=True)
class AdyenCreditResult:
    event_code: str
    psp_reference: str
    workspace_id: str | None = None
    amount_microdollars: int = 0
    credited: bool = False
    ignored: bool = False
    manual_review: bool = False


@dataclass(frozen=True)
class AdyenCheckoutReference:
    workspace_id: str
    credit_amount_cents: int
    charge_amount_cents: int


@dataclass(frozen=True)
class PreparedAdyenNotification:
    result: AdyenCreditResult
    merchant_reference: str | None = None
    reference: AdyenCheckoutReference | None = None


def create_adyen_checkout_session(
    *,
    body: CheckoutRequest,
    workspace_id: str,
    customer_email: str | None,
    settings: Settings,
) -> dict[str, Any]:
    """Create an embedded Adyen session without changing any balance."""
    if not settings.adyen_checkout_ready:
        raise api_error(400, "Adyen checkout is not configured", ErrorType.BAD_REQUEST)
    if STORE.get_workspace(workspace_id) is None:
        raise api_error(404, "Workspace not found", ErrorType.NOT_FOUND)

    credit_amount_cents = dollars_to_cents(body.amount)
    fee = processing_fee(
        credit_amount_cents=credit_amount_cents,
        variable_basis_points=settings.adyen_card_fee_basis_points,
        fixed_fee_cents=settings.adyen_card_fee_fixed_cents,
        minimum_fee_cents=settings.checkout_card_fee_minimum_cents,
    )
    merchant_reference = _new_checkout_reference(
        workspace_id=workspace_id,
        credit_amount_cents=fee.credit_amount_cents,
        charge_amount_cents=fee.charge_amount_cents,
        reference_key=str(settings.adyen_reference_key),
    )
    return_url = body.success_url or (
        f"https://{settings.trusted_domain}/console/credits?checkout=processing"
    )
    payload: dict[str, Any] = {
        "amount": {"currency": "USD", "value": fee.charge_amount_cents},
        "channel": "Web",
        "countryCode": "US",
        "lineItems": _checkout_line_items(fee),
        "merchantAccount": settings.adyen_merchant_account,
        "mode": "embedded",
        "reference": merchant_reference,
        "returnUrl": return_url,
        "shopperLocale": "en-US",
        "shopperStatement": "TrustedRouter credits",
    }
    if customer_email:
        payload["shopperEmail"] = customer_email

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                _checkout_sessions_url(settings),
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid.uuid4()),
                    "X-API-Key": str(settings.adyen_api_key),
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise api_error(
            503,
            "Adyen checkout is temporarily unavailable",
            ErrorType.SERVICE_UNAVAILABLE,
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise api_error(
            502,
            "Adyen returned an invalid response",
            ErrorType.INTERNAL_ERROR,
        ) from exc
    if response.is_error:
        _raise_adyen_checkout_error(response.status_code, data)
    if not isinstance(data, dict):
        raise api_error(502, "Adyen returned an invalid response", ErrorType.INTERNAL_ERROR)
    session_id = data.get("id")
    session_data = data.get("sessionData")
    if not isinstance(session_id, str) or not isinstance(session_data, str):
        raise api_error(
            502,
            "Adyen returned an incomplete checkout session",
            ErrorType.INTERNAL_ERROR,
        )
    return {
        "id": session_id,
        "session_data": session_data,
        "client_key": settings.adyen_client_key,
        "environment": "test" if settings.adyen_environment == "test" else "live-us",
        "workspace_id": workspace_id,
        **money_pair("amount", fee.credit_amount_microdollars),
        **money_pair("processing_fee", fee.processing_fee_microdollars),
        **money_pair("total", fee.charge_amount_microdollars),
        "mode": "adyen",
    }


def adyen_web_asset_urls(settings: Settings) -> tuple[str, str]:
    environment = "test" if settings.adyen_environment == "test" else "live-us"
    root = f"https://checkoutshopper-{environment}.cdn.adyen.com/checkoutshopper/sdk/{settings.adyen_web_version}"
    return f"{root}/adyen.js", f"{root}/adyen.css"


def notification_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_items = payload.get("notificationItems")
    if not isinstance(raw_items, list) or not raw_items:
        raise api_error(400, "Invalid Adyen webhook payload", ErrorType.BAD_REQUEST)
    items: list[Mapping[str, Any]] = []
    for wrapper in raw_items:
        if not isinstance(wrapper, Mapping):
            raise api_error(400, "Invalid Adyen webhook payload", ErrorType.BAD_REQUEST)
        item = wrapper.get("NotificationRequestItem")
        if not isinstance(item, Mapping):
            raise api_error(400, "Invalid Adyen webhook payload", ErrorType.BAD_REQUEST)
        items.append(item)
    return items


def verify_adyen_notification(item: Mapping[str, Any], settings: Settings) -> None:
    if not settings.adyen_webhook_ready or not settings.adyen_hmac_key:
        raise api_error(
            503,
            "Adyen webhook verification is not configured",
            ErrorType.SERVICE_UNAVAILABLE,
        )
    additional_data = item.get("additionalData")
    if not isinstance(additional_data, Mapping):
        raise api_error(400, "Invalid Adyen webhook signature", ErrorType.BAD_REQUEST)
    received = additional_data.get("hmacSignature")
    if not isinstance(received, str):
        raise api_error(400, "Invalid Adyen webhook signature", ErrorType.BAD_REQUEST)
    expected = adyen_notification_signature(item, settings.adyen_hmac_key)
    if not hmac.compare_digest(received, expected):
        raise api_error(400, "Invalid Adyen webhook signature", ErrorType.BAD_REQUEST)


def adyen_notification_signature(item: Mapping[str, Any], hmac_key_hex: str) -> str:
    amount = item.get("amount")
    amount_mapping = amount if isinstance(amount, Mapping) else {}
    values = (
        item.get("pspReference"),
        item.get("originalReference"),
        item.get("merchantAccountCode"),
        item.get("merchantReference"),
        amount_mapping.get("value"),
        amount_mapping.get("currency"),
        item.get("eventCode"),
        item.get("success"),
    )
    payload = ":".join(_escape_hmac_field(_string_field(value)) for value in values)
    try:
        key = bytes.fromhex(hmac_key_hex)
    except ValueError as exc:
        raise api_error(
            503,
            "Adyen webhook verification is misconfigured",
            ErrorType.SERVICE_UNAVAILABLE,
        ) from exc
    if not key:
        raise api_error(
            503,
            "Adyen webhook verification is misconfigured",
            ErrorType.SERVICE_UNAVAILABLE,
        )
    digest = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def credit_adyen_notification(
    item: Mapping[str, Any],
    *,
    live: bool,
    settings: Settings,
) -> AdyenCreditResult:
    """Validate and apply one HMAC-preverified webhook notification."""
    prepared = prepare_adyen_notification(item, live=live, settings=settings)
    return apply_adyen_notification(prepared)


def prepare_adyen_notification(
    item: Mapping[str, Any],
    *,
    live: bool,
    settings: Settings,
) -> PreparedAdyenNotification:
    """Validate one item without mutating credits or other durable state."""
    event_code = _required_string(item, "eventCode")
    psp_reference = _required_string(item, "pspReference")
    expected_live = settings.adyen_environment == "live"
    if live != expected_live:
        raise api_error(400, "Adyen webhook environment mismatch", ErrorType.BAD_REQUEST)
    if item.get("merchantAccountCode") != settings.adyen_merchant_account:
        raise api_error(400, "Adyen webhook merchant mismatch", ErrorType.BAD_REQUEST)

    success = _string_field(item.get("success")).lower() == "true"
    if event_code != "AUTHORISATION" or not success:
        return PreparedAdyenNotification(
            result=AdyenCreditResult(
                event_code=event_code,
                psp_reference=psp_reference,
                ignored=True,
                manual_review=event_code in _ADVERSE_EVENT_CODES,
            )
        )

    merchant_reference = _required_string(item, "merchantReference")
    reference = _parse_checkout_reference(
        merchant_reference,
        reference_key=str(settings.adyen_reference_key),
    )
    amount = item.get("amount")
    if not isinstance(amount, Mapping):
        raise api_error(400, "Adyen webhook amount is missing", ErrorType.BAD_REQUEST)
    if amount.get("currency") != "USD":
        raise api_error(400, "Adyen webhook currency mismatch", ErrorType.BAD_REQUEST)
    value = amount.get("value")
    if not isinstance(value, int) or isinstance(value, bool):
        raise api_error(400, "Adyen webhook amount is invalid", ErrorType.BAD_REQUEST)
    if value != reference.charge_amount_cents:
        raise api_error(400, "Adyen webhook amount mismatch", ErrorType.BAD_REQUEST)
    if STORE.get_credit_account(reference.workspace_id) is None:
        raise api_error(404, "Credit account not found", ErrorType.NOT_FOUND)

    return PreparedAdyenNotification(
        result=AdyenCreditResult(
            event_code=event_code,
            psp_reference=psp_reference,
            workspace_id=reference.workspace_id,
            amount_microdollars=reference.credit_amount_cents * 10_000,
        ),
        merchant_reference=merchant_reference,
        reference=reference,
    )


def apply_adyen_notification(
    prepared: PreparedAdyenNotification,
) -> AdyenCreditResult:
    """Apply a fully validated notification to the typed credit ledger."""
    result = prepared.result
    if result.manual_review:
        log.warning(
            "adyen.payment_requires_manual_review",
            extra={
                "event": "adyen.payment_requires_manual_review",
                "event_code": result.event_code,
                "psp_reference_fingerprint": _fingerprint(result.psp_reference),
            },
        )
    if prepared.reference is None or prepared.merchant_reference is None:
        return result

    # The signed merchant reference carries no initiating user (its shape is
    # fixed by the HMAC scheme), so a real Adyen purchase accrues lifetime
    # top-up to the workspace owner — the same fallback every other payment
    # path uses when the initiator is unknown. Without this an Adyen payer
    # would stay funding-gated for phone verification.
    workspace = STORE.get_workspace(prepared.reference.workspace_id)
    credited = STORE.credit_workspace_typed_direct(
        prepared.reference.workspace_id,
        result.amount_microdollars,
        f"adyen_checkout:{prepared.merchant_reference}",
        lifetime_topup_user_id=(workspace.owner_user_id if workspace is not None else None),
    )
    if credited:
        record_credit_purchase(
            prepared.reference.workspace_id,
            amount_microdollars=result.amount_microdollars,
            payment_method="adyen",
        )
    return AdyenCreditResult(
        event_code=result.event_code,
        psp_reference=result.psp_reference,
        workspace_id=result.workspace_id,
        amount_microdollars=result.amount_microdollars,
        credited=credited,
    )


def _checkout_sessions_url(settings: Settings) -> str:
    version = settings.adyen_checkout_api_version
    if settings.adyen_environment == "test":
        return f"https://checkout-test.adyen.com/v{version}/sessions"
    return (
        f"https://{settings.adyen_live_endpoint_prefix}-checkout-live.adyenpayments.com"
        f"/checkout/v{version}/sessions"
    )


def _checkout_line_items(fee: ProcessingFee) -> list[dict[str, Any]]:
    items = [_line_item("credits", "TrustedRouter credits", fee.credit_amount_cents)]
    if fee.processing_fee_cents:
        items.append(
            _line_item("processing-fee", "Payment processing fee", fee.processing_fee_cents)
        )
    return items


def _line_item(item_id: str, description: str, amount_cents: int) -> dict[str, Any]:
    return {
        "id": item_id,
        "description": description,
        "quantity": 1,
        "amountExcludingTax": amount_cents,
        "amountIncludingTax": amount_cents,
        "taxAmount": 0,
        "taxPercentage": 0,
    }


def _new_checkout_reference(
    *,
    workspace_id: str,
    credit_amount_cents: int,
    charge_amount_cents: int,
    reference_key: str,
) -> str:
    try:
        canonical_workspace_id = str(uuid.UUID(workspace_id))
    except ValueError as exc:
        raise api_error(400, "Invalid workspace id", ErrorType.BAD_REQUEST) from exc
    unsigned = (
        f"trc_{uuid.UUID(canonical_workspace_id).hex}_"
        f"{_base36(credit_amount_cents)}_{_base36(charge_amount_cents)}_"
        f"{uuid.uuid4().hex[:8]}"
    )
    return f"{unsigned}_{_reference_signature(unsigned, reference_key)}"


def _parse_checkout_reference(
    reference: str, *, reference_key: str
) -> AdyenCheckoutReference:
    match = _CHECKOUT_REFERENCE_RE.fullmatch(reference)
    if match is None:
        raise api_error(400, "Invalid Adyen checkout reference", ErrorType.BAD_REQUEST)
    expected = _reference_signature(match.group("unsigned"), reference_key)
    if not hmac.compare_digest(match.group("signature"), expected):
        raise api_error(400, "Invalid Adyen checkout reference", ErrorType.BAD_REQUEST)
    return AdyenCheckoutReference(
        workspace_id=str(uuid.UUID(hex=match.group("workspace"))),
        credit_amount_cents=int(match.group("credit"), 36),
        charge_amount_cents=int(match.group("charge"), 36),
    )


def _reference_signature(unsigned: str, reference_key: str) -> str:
    if len(reference_key) < 32:
        raise api_error(
            503,
            "Adyen checkout reference signing is misconfigured",
            ErrorType.SERVICE_UNAVAILABLE,
        )
    return hmac.new(
        reference_key.encode("utf-8"),
        unsigned.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:16]


def _base36(value: int) -> str:
    if value <= 0:
        raise ValueError("base36 value must be positive")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def _raise_adyen_checkout_error(status_code: int, data: Any) -> None:
    error_code = data.get("errorCode") if isinstance(data, Mapping) else None
    message = data.get("message") if isinstance(data, Mapping) else None
    if status_code == 422 and str(error_code) == "901":
        raise api_error(
            503,
            "Adyen merchant account is not active",
            ErrorType.SERVICE_UNAVAILABLE,
        )
    if status_code in {401, 403}:
        raise api_error(503, "Adyen checkout is misconfigured", ErrorType.SERVICE_UNAVAILABLE)
    if status_code == 429 or status_code >= 500:
        raise api_error(
            503,
            "Adyen checkout is temporarily unavailable",
            ErrorType.SERVICE_UNAVAILABLE,
        )
    safe_message = "Adyen rejected the checkout request"
    if isinstance(message, str) and message in {
        "Invalid Merchant Account",
        "No payment methods available",
    }:
        safe_message = message
    raise api_error(400, safe_message, ErrorType.BAD_REQUEST)


def _required_string(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise api_error(400, f"Adyen webhook {key} is missing", ErrorType.BAD_REQUEST)
    return value


def _string_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape_hmac_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
