"""NYTE 2027 pre-sale. Stripe owns the order; this never creates credits."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

import stripe
from fastapi import BackgroundTasks
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from trusted_router.config import Settings
from trusted_router.content import token_index
from trusted_router.errors import api_error
from trusted_router.services.email import EmailMessage, get_email_service
from trusted_router.services.stripe_fees import processing_fee
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

KIND = "reserved_tokens_2027"
TERMS_VERSION = "2026-10-28-v6"
LEGACY_TERMS_VERSIONS = {"2026-10-27-v3", "2026-10-27-v4", "2026-10-27-v5"}
FEE_FIELDS = (
    "exchange_fee_bps", "exchange_fee_cents", "processing_fee_cents",
    "processing_fee_bps", "processing_fee_fixed_cents",
)
WINDOW = {
    "usable_from": "2027-01-01",
    "usable_until": "2027-12-31",
    "refund_window_days": "30",
    "transfer_policy": "nyte_resale_only",
    "resale_fee_bps": "500",
    "resale_priority": "ahead_of_new_inventory",
    "resale_payout_method": "original_card_refund",
    "resale_payout_cap": "original_lot_purchase_price",
    "terms_version": TERMS_VERSION,
}
log = logging.getLogger("trusted_router.acquisition")


class ReservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grade: Literal["AAA", "A", "B", "C"]
    lots: StrictInt
    terms_accepted: StrictBool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def reservation_quote(settings: Settings, grade: str, lots: int) -> dict[str, Any]:
    discount = settings.reserved_tokens_discount_bps
    if not settings.reserved_tokens_checkout_enabled or discount is None:
        raise api_error(503, "Reserved token checkout is not enabled or its discount is not configured", ErrorType.SERVICE_UNAVAILABLE)
    exchange_fee_bps = settings.reserved_tokens_exchange_fee_bps
    if exchange_fee_bps is None:
        raise api_error(503, "Reserved token checkout is not enabled or its exchange fee is not configured", ErrorType.SERVICE_UNAVAILABLE)
    if grade not in {"AAA", "A", "B", "C"}:
        raise api_error(422, "Choose an NYTE Index grade: AAA, A, B or C", ErrorType.BAD_REQUEST)
    if type(lots) is not int or not 1 <= lots <= settings.reserved_tokens_max_lots_per_checkout:
        raise api_error(422, f"Choose 1–{settings.reserved_tokens_max_lots_per_checkout} lots; book a call for larger orders", ErrorType.BAD_REQUEST)
    try:
        price_age_days = (_utc_now().date() - date.fromisoformat(token_index.AS_OF_ISO)).days
    except (TypeError, ValueError):
        price_age_days = -1
    if not 0 <= price_age_days <= settings.reserved_tokens_max_price_age_days:
        raise api_error(503, "Reserved token prices are being updated; book a call", ErrorType.SERVICE_UNAVAILABLE)
    list_cents = Decimal(str(token_index.QUOTES[grade]["value"])) * 100
    per_lot = int((list_cents * (10_000 - discount) / 10_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if per_lot <= 0:
        raise api_error(503, "Reservation amount is outside card checkout limits; book a call", ErrorType.SERVICE_UNAVAILABLE)
    subtotal = per_lot * lots
    exchange_fee = (subtotal * exchange_fee_bps + 5_000) // 10_000
    card_fee = processing_fee(
        credit_amount_cents=subtotal + exchange_fee,
        variable_basis_points=settings.stripe_card_fee_basis_points,
        fixed_fee_cents=settings.stripe_card_fee_fixed_cents,
        minimum_fee_cents=0, max_fee_cents=None, fee_multiplier_basis_points=10_000,
    ).processing_fee_cents
    total = subtotal + exchange_fee + card_fee
    if not 50 <= total <= 99_999_999:
        raise api_error(503, "Reservation amount is outside card checkout limits; book a call", ErrorType.SERVICE_UNAVAILABLE)
    return {
        "grade": grade, "lots": lots, "per_lot_cents": per_lot,
        "list_price_cents": int(list_cents.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
        "discount_bps": discount, "index_as_of": token_index.AS_OF_ISO,
        "subtotal_cents": subtotal, "exchange_fee_bps": exchange_fee_bps,
        "exchange_fee_cents": exchange_fee, "processing_fee_cents": card_fee,
        "processing_fee_bps": settings.stripe_card_fee_basis_points,
        "processing_fee_fixed_cents": settings.stripe_card_fee_fixed_cents,
        "total_cents": total,
        "max_lots": settings.reserved_tokens_max_lots_per_checkout, **WINDOW,
        "resale_fee_bps": str(settings.resale_fee_bps),
    }


def create_reservation_checkout(settings: Settings, body: ReservationRequest) -> dict[str, str]:
    quote = reservation_quote(settings, body.grade, body.lots)
    if body.terms_accepted is not True:
        raise api_error(422, "Accept the reserved tokens terms before checkout", ErrorType.BAD_REQUEST)
    if not settings.stripe_secret_key:
        raise api_error(503, "Card checkout is not configured", ErrorType.SERVICE_UNAVAILABLE)
    metadata = {"kind": KIND, **{key: str(value) for key, value in quote.items() if key != "max_lots"}}
    # Use the same SDK/key as stripe_billing, but no workspace, saved payment
    # method, top-up surcharge, mock purchase or credit-acquisition callbacks.
    line_items: list[Any] = [{"price_data": {
            "currency": "usd", "unit_amount": quote["per_lot_cents"],
            "product_data": {
                "name": f"Reserved tokens · Grade {body.grade} · 1B tokens",
                "description": "Usable January 1, 2027 through December 31, 2027. Unused tokens expire December 31, 2027. Full refund including fees on request within 30 days of purchase. After 30 days, exit only through NYTE resale. Transfer only through NYTE resale: list lots; NYTE resells ahead of new inventory. Each sale refunds the resale price minus a 5% fee to the original card, capped at what you paid for those lots, excluding purchase fees.",
            },
        }, "quantity": body.lots}]
    for key, name in (("exchange_fee_cents", "NYTE exchange fee (0.1%)"), ("processing_fee_cents", "Card processing fee")):
        if quote[key]:
            line_items.append({"price_data": {
                "currency": "usd", "unit_amount": quote[key], "product_data": {"name": name},
            }, "quantity": 1})
    session = stripe.checkout.Session.create(
        api_key=settings.stripe_secret_key,
        mode="payment", payment_method_types=["card"], customer_creation="always",
        line_items=line_items,
        metadata=metadata, payment_intent_data={"metadata": metadata},
        success_url="https://nytokenexchange.com/buy?reserved=success",
        cancel_url=f"https://nytokenexchange.com/buy?step=2&grade={body.grade}&unit=blocks",
    )
    return {"url": str(session["url"])}


def reservation_position_id(session_id: str) -> str:
    """Stable across retries/releases; Stripe session entropy prevents guessing.

    Domain-separated SHA-256 hides the Stripe identifier without a rotating
    secret or a new mapping table. This reference is not an access credential.
    """
    digest = hashlib.sha256(f"nyte:reserved-position:v1:{session_id}".encode()).hexdigest()
    return f"nyte_pos_{digest}"


def session_amounts(metadata: dict[str, Any]) -> tuple[int, int]:
    """Validate a paid checkout snapshot without consulting current prices/settings."""
    try:
        if metadata["terms_version"] not in LEGACY_TERMS_VERSIONS | {TERMS_VERSION}:
            raise ValueError("Invalid reservation terms version")
        lots, per_lot = int(metadata["lots"]), int(metadata["per_lot_cents"])
        legacy_no_fees = (
            metadata["terms_version"] in LEGACY_TERMS_VERSIONS
            and not any(key in metadata for key in FEE_FIELDS)
        )
        if legacy_no_fees:
            fees = dict.fromkeys(FEE_FIELDS, 0)
        else:
            # Stripe metadata is strings. Refuse partial, negative or fractional
            # fees; retain the checkout snapshot rather than today's price/rates.
            if not all(isinstance(metadata[key], str) and metadata[key].isascii() and metadata[key].isdigit() for key in FEE_FIELDS):
                raise ValueError("Invalid fee metadata")
            fees = {key: int(metadata[key]) for key in FEE_FIELDS}
            if metadata["exchange_fee_bps"] != "10" or fees["processing_fee_bps"] >= 10_000:
                raise ValueError("Invalid fee rates")
        subtotal = lots * per_lot
        total = subtotal + fees["exchange_fee_cents"] + fees["processing_fee_cents"]
        if not legacy_no_fees and (metadata["subtotal_cents"] != str(subtotal) or metadata["total_cents"] != str(total)):
            raise ValueError("Invalid fee totals")
        if lots <= 0 or per_lot <= 0:
            raise ValueError("Invalid reservation quantity or price")
        return subtotal, total
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid reserved token payment amounts") from exc


def handle_reservation_event(event: dict[str, Any], settings: Settings, background: BackgroundTasks | None = None) -> dict[str, Any]:
    """Called before ALL credit handlers, independent of the sales feature flag.

    A durable session lease deduplicates different event IDs and concurrent
    deliveries. SES has no idempotency key: a crash after SES accepts mail but
    before the sent marker commits can repeat that email on retry. Stripe is
    authoritative; no payment or allowance is repeated.
    """
    obj = event["data"]["object"]
    event_type = event.get("type")
    result: dict[str, Any] = {"reserved_tokens": True, "credited": False}
    if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        return {**result, "ignored": True}
    if obj.get("payment_status") != "paid":
        return {**result, "payment_pending": True}
    metadata = obj.get("metadata") or {}
    try:
        subtotal, total = session_amounts(metadata)
        valid = (
            obj.get("amount_total") == total
            and obj.get("currency") == "usd" and obj.get("mode") == "payment"
            and metadata["grade"] in {"AAA", "A", "B", "C"}
            and all(metadata[key] == value for key, value in WINDOW.items() if key not in {"terms_version", "resale_fee_bps"})
            and metadata["resale_fee_bps"] == "500"
            and int(metadata["list_price_cents"]) > 0
            and 0 <= int(metadata["discount_bps"]) < 10_000
            and isinstance(metadata["index_as_of"], str)
            and isinstance(obj.get("id"), str) and obj["id"].startswith("cs_")
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        valid = False
    if not valid:
        raise api_error(400, "Invalid reserved token payment amount or metadata", ErrorType.BAD_REQUEST)
    session_id = obj["id"]
    position_id = reservation_position_id(session_id)
    owner = uuid.uuid4().hex
    claim, purchase_timestamp = STORE.claim_reserved_notification(session_id, owner, now=int(_utc_now().timestamp()))
    if settings.resale_enabled:
        from trusted_router.services.resale import record_purchase, retry_buyer_refunds

        position = record_purchase(obj, event["id"], None if claim == "sent" else purchase_timestamp, settings, backfill=claim == "sent")
        # Allocation is already durable. Refund transport/alert failures cannot
        # roll it back or send the purchase into a credit handler.
        if background is not None:
            background.add_task(retry_buyer_refunds, position, settings)
        else:
            retry_buyer_refunds(position, settings)
    if claim == "sent":
        return {**result, "duplicate": True}
    if claim == "busy":
        raise api_error(503, "Reservation notification is being processed; retry", ErrorType.SERVICE_UNAVAILABLE)
    purchased_at = datetime.fromtimestamp(purchase_timestamp, UTC)
    refund_deadline = purchased_at + timedelta(days=int(WINDOW["refund_window_days"]))
    details = {key: metadata[key] for key in ("grade", "lots", "per_lot_cents", "list_price_cents", "discount_bps", "index_as_of", *WINDOW)}
    details.update({key: str(int(metadata.get(key, "0"))) for key in FEE_FIELDS})
    details.update(subtotal_cents=str(subtotal), total_cents=str(total))
    details.update(position_id=position_id, purchase_completed_at=purchased_at.isoformat(), refund_deadline_at=refund_deadline.isoformat(), stripe_session_id=session_id, stripe_customer_id=obj.get("customer") if isinstance(obj.get("customer"), str) else None)
    try:
        if STORE.record_webhook_event_once(KIND, session_id):
            log.info("acquisition.reserved_tokens_recorded", extra={"event": "acquisition.reserved_tokens_recorded", **details})
        sent = get_email_service(settings).send(EmailMessage(
            to=settings.partner_inquiry_email or "enterprise@trustedrouter.com",
            subject=f"NYTE 2027 reservation · {position_id}",
            text_body="Paid reserved tokens (Stripe is the order record):\n" + json.dumps(details, sort_keys=True, indent=2),
            mail_class="reserved_tokens",
        ))
        if not sent:
            raise api_error(503, "Reservation notification unavailable; retry", ErrorType.SERVICE_UNAVAILABLE)
    except Exception:
        STORE.finish_reserved_notification(session_id, owner, sent=False)
        raise
    STORE.finish_reserved_notification(session_id, owner, sent=True)
    return {**result, "recorded": True}
