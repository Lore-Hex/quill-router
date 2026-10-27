"""Holder capabilities and the Stripe refund outbox. Never issues balance credits."""
from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

import sentry_sdk
import stripe
from email_validator import validate_email

from trusted_router.config import Settings
from trusted_router.security import constant_time_equal, signed_token_signature
from trusted_router.services.email import EmailMessage, get_email_service
from trusted_router.storage import STORE
from trusted_router.storage_resale import reference

_PRIVATE_STRIPE_READ: ContextVar[bool] = ContextVar("nyte_private_stripe_read", default=False)


class _PrivateStripeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (_PRIVATE_STRIPE_READ.get() and record.name.startswith(("stripe", "urllib3", "httpx", "httpcore", "botocore", "boto3")))


# stripe-python logs GET query strings at INFO, including Customer.list(email).
# Suppress SDK/transport records in this request's context, not other threads.
for _logger_name in ("stripe", "urllib3.connectionpool", "httpx"):
    logging.getLogger(_logger_name).addFilter(_PrivateStripeLogFilter())


@lru_cache(maxsize=1)
def _private_sentry_client() -> sentry_sdk.Client:
    # An explicitly empty DSN prevents SENTRY_DSN environment fallback.
    # No integrations/transport means no outbound URL query spans or SDK logs.
    return sentry_sdk.Client(dsn="", default_integrations=False, auto_enabling_integrations=False,
                             enable_logs=False, traces_sample_rate=0.0)


@contextmanager
def _private_provider_logs() -> Iterator[None]:
    # Also cover SDK child loggers and SES payload diagnostics at the root
    # sinks. This filter remains inert outside the current sensitive call.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _PrivateStripeLogFilter) for f in handler.filters):
            handler.addFilter(_PrivateStripeLogFilter())
    marker = _PRIVATE_STRIPE_READ.set(True)
    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_client(_private_sentry_client())
            scope.span = None
            yield
    finally:
        _PRIVATE_STRIPE_READ.reset(marker)


@contextmanager
def _private_stripe_reads() -> Iterator[None]:
    from stripe import _util

    # Stripe's optional console logger writes directly to stderr, bypassing
    # Python filters. Refuse private Stripe calls under that diagnostic setting.
    if stripe.log in {"debug", "info"} or _util.STRIPE_LOG in {"debug", "info"}:
        raise ValueError("Private receipt lookup unavailable while Stripe console diagnostics are enabled")
    with _private_provider_logs():
        yield


log = logging.getLogger(__name__)
GENERIC_RESPONSE = "if we have reservations for that address, we sent a link"


def command(op: str, **kwargs: Any) -> dict[str, Any]:
    return STORE.reserved_resale({"op": op, "now": int(time.time()), **kwargs})


def holder_key(email: str) -> str:
    return reference("nyte-holder:" + email.strip().lower())


def record_purchase(obj: dict[str, Any], event_id: str, purchased_at: int | None, settings: Settings, *, backfill: bool = False, needs_review: bool = False, payment_state: dict[str, Any] | None = None) -> dict[str, Any]:
    from trusted_router.services.reserved_tokens import reservation_position_id

    if payment_state is None:
        payment_state = current_payment_state(obj.get("payment_intent"), settings)
    metadata = obj["metadata"]
    email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or ""
    # Missing contact/payment data still creates the paid position. It cannot
    # be listed until reconciled; Stripe's normal completed event has both.
    return command("purchase", position={
        "id": reservation_position_id(obj["id"]), "session_id": obj["id"],
        "grade": metadata["grade"], "lots": int(metadata["lots"]),
        "per_lot_cents": int(metadata["per_lot_cents"]), "purchase_completed_at": purchased_at,
        "payment_intent": obj.get("payment_intent"), "holder": holder_key(email),
        "terms_version": metadata["terms_version"], "status": "needs_review" if needs_review else "held",
    }, event_id=event_id, backfill=backfill, allocate=settings.resale_enabled and not backfill,
        fee_bps=settings.resale_fee_bps, payment_state=payment_state)


def alert_ops(settings: Settings, subject: str, body: str) -> None:
    try:
        sent = get_email_service(settings).send(EmailMessage(
            to=settings.partner_inquiry_email or "enterprise@trustedrouter.com",
            subject=subject, text_body=body, mail_class="nyte_resale",
        ))
        if not sent:
            log.error("nyte.resale_ops_email_unavailable")
    except Exception:  # noqa: BLE001 - never log Stripe/SES exceptions or PII
        log.error("nyte.resale_ops_email_unavailable")


@_private_stripe_reads()
def process_fill(fill_id: str, settings: Settings) -> None:
    owner = secrets.token_hex(16)
    fill = command("fill_claim", fill=fill_id, owner=owner)
    if not fill:
        return
    refund_id = fill.get("stripe_refund_id")
    status = "retry"
    options: dict[str, Any] = {"api_key": settings.stripe_secret_key}
    try:
        if not settings.stripe_secret_key:
            raise ValueError("Stripe unavailable")
        if fill["refund_amount_cents"] == 0:
            status = "refunded"
        else:
            refund: Any = None
            if refund_id:
                refund = _stripe_dict(stripe.Refund.retrieve(refund_id, **options))
            else:
                # Reconcile before creating, including a crash after Stripe's
                # success but before our commit. Never reuse an expired Stripe
                # idempotency key blindly (Stripe retains keys >=24 hours).
                for count, candidate in enumerate(stripe.Refund.list(payment_intent=fill["payment_intent"], limit=100, **options).auto_paging_iter()):
                    if count >= 1000:
                        raise ValueError("Refund reconciliation requires ops")
                    candidate_row = _stripe_dict(candidate)
                    metadata = candidate_row.get("metadata") or {}
                    if (metadata.get("fill_id") or metadata.get("nyte_fill_id")) == fill_id:
                        refund = candidate_row
                        break
                if refund is None:
                    if int(time.time()) - fill["first_attempt_at"] >= 23 * 3600:
                        raise ValueError("Ambiguous old attempt requires ops reconciliation")
                    refund = stripe.Refund.create(
                        payment_intent=fill["payment_intent"], amount=fill["refund_amount_cents"],
                        metadata={"kind": "reserved_tokens_2027", "fill_id": fill_id,
                                  "position_id": fill["position_id"], "nyte_fill_id": fill_id},
                        idempotency_key="nyte-resale-" + fill_id,
                        **options,
                    )
            refund = _stripe_dict(refund)
            refund_id = refund["id"]
            if refund.get("amount") != fill["refund_amount_cents"] or refund.get("payment_intent") != fill["payment_intent"]:
                raise ValueError("Refund reconciliation mismatch")
            status = "refunded" if refund.get("status") == "succeeded" else "needs_ops" if refund.get("status") in {"failed", "canceled"} else "retry"
    except Exception:  # noqa: BLE001 - preserve retryability without PII/error bodies
        status = "retry"
    finished = command("fill_finish", fill=fill_id, owner=owner, status=status, stripe_refund_id=refund_id,
                       failure_status=refund.get("status") if status == "needs_ops" else None)
    if status == "needs_ops" and finished:
        alert_failed_refund(finished, settings)
    elif status != "refunded":
        alert_ops(settings, "NYTE resale refund needs retry", f"Fill {fill_id} is pending. Run python -m scripts.retry_nyte_resale_refunds. Never issue a second refund without reconciling this fill's Stripe metadata.")


def retry_buyer_refunds(position: dict[str, Any], settings: Settings) -> None:
    for fill_id in position.get("fills", []):
        try:
            process_fill(fill_id, settings)
        except Exception:  # noqa: BLE001 - ledger commit already succeeded
            alert_ops(settings, "NYTE resale refund worker unavailable", f"Retry durable fill {fill_id}.")


def _signature(settings: Settings, payload: str) -> str:
    # Reuses OAuth's domain-separated signed-token utility, with the existing
    # control Stripe secret. Key rotation invalidates outstanding 15-minute links.
    return signed_token_signature(settings.stripe_secret_key or "", "nyte-holder-v1", payload)


def send_holder_link(email: str, settings: Settings) -> None:
    """Always mail a capability, even to an address with no positions.

    No lookup-dependent HTTP status, body, or synchronous Stripe work. The email
    itself is transient contact data, never stored on an analytics/event row.
    """
    if not settings.resale_enabled:
        return
    try:
        email = validate_email(email, check_deliverability=False).normalized.lower()
        nonce, expires = secrets.token_urlsafe(32), int(time.time()) + 900
        payload = f"{nonce}.{expires}"
        token = payload + "." + _signature(settings, payload)
        command("token_create", key=reference(token), holder=holder_key(email))
        # Fragment never reaches access logs, Referer, or analytics. Redemption
        # requires an explicit button so mail link scanners cannot consume it.
        link = "https://nytokenexchange.com/manage-reservation#" + token
        with _private_provider_logs():
            get_email_service(settings).send(EmailMessage(
                to=email, subject="Manage your NYTE reservations",
                text_body=f"Open this single-use link within 15 minutes:\n{link}\nIf you have no reservations, the page will show none.",
                mail_class="nyte_holder",
            ))
    except Exception:  # noqa: BLE001 - uniform response, no submitted address in logs
        log.error("nyte.holder_link_unavailable")


def redeem_link(token: str, settings: Settings) -> dict[str, str]:
    try:
        payload, signature = token.rsplit(".", 1)
        if len(token) > 256 or not constant_time_equal(signature, _signature(settings, payload)) or int(payload.rsplit(".", 1)[1]) <= int(time.time()):
            raise ValueError("Invalid link")
    except (ValueError, TypeError) as exc:
        raise ValueError("Link expired or invalid; request a new link") from exc
    session = secrets.token_urlsafe(32)
    command("token_consume", key=reference(token), session=reference(session))
    return {"session": session}


@_private_stripe_reads()
def backfill_email(email: str, holder: str, settings: Settings) -> None:
    """Authenticated, bounded Stripe reads, not a production entity body scan.

    Retrieve exact-email customers and their completed sessions. The old
    reserved_notification row supplies first-paid-webhook time. If missing,
    preserve an unknown time and require ops; NEVER use session.created.
    """
    from trusted_router.services.reserved_tokens import KIND

    if holder_key(email) != holder:
        raise ValueError("Email does not match this link")
    for customer_count, customer in enumerate(stripe.Customer.list(email=email.strip(), limit=100, api_key=settings.stripe_secret_key).auto_paging_iter()):
        if customer_count >= 100:
            raise ValueError("Contact ops to reconcile additional purchases")
        for count, stripe_session in enumerate(stripe.checkout.Session.list(customer=customer["id"], status="complete", limit=100, api_key=settings.stripe_secret_key).auto_paging_iter()):
            if count >= 1000:
                raise ValueError("Contact ops to reconcile additional purchases")
            obj = _stripe_dict(stripe_session)
            m = obj.get("metadata") or {}
            if m.get("kind") != KIND or obj.get("payment_status") != "paid":
                continue
            actual_email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or ""
            if holder_key(actual_email) != holder:
                continue
            if obj.get("currency") != "usd" or obj.get("mode") != "payment" or m.get("grade") not in {"AAA", "A", "B", "C"}:
                continue
            if int(m["lots"]) <= 0 or int(m["per_lot_cents"]) <= 0 or obj.get("amount_total") != int(m["lots"]) * int(m["per_lot_cents"]):
                continue
            # Checkout stays paid after refunds. Reconcile the original card
            # charge before importing; a refunded/disputed order needs review.
            state = current_payment_state(obj.get("payment_intent"), settings)
            record_purchase(obj, "backfill:" + obj["id"], None, settings, backfill=True, payment_state=state)


@_private_stripe_reads()
def current_payment_state(payment_intent: str | None, settings: Settings) -> dict[str, Any]:
    if not payment_intent:
        return {"unpaid": True}
    payment = _stripe_dict(stripe.PaymentIntent.retrieve(payment_intent, expand=["latest_charge"],
                                                        api_key=settings.stripe_secret_key))
    state = payment_state(payment_intent, payment.get("latest_charge"), settings)
    state["unpaid"] = state["unpaid"] or payment.get("status") != "succeeded"
    return state


def payment_state(payment_intent: str, charge: Any, settings: Settings) -> dict[str, Any]:
    """Bounded Stripe refund evidence, verified against durable fills in the transaction."""
    if not isinstance(charge, dict):
        return {"unpaid": True}
    state: dict[str, Any] = {"unpaid": not charge.get("paid"), "disputed": bool(charge.get("disputed")),
                             "amount_refunded": int(charge.get("amount_refunded", 0)), "refunds": []}
    if state["amount_refunded"] and not state["disputed"]:
        for count, refund in enumerate(stripe.Refund.list(payment_intent=payment_intent, limit=100,
                                                          api_key=settings.stripe_secret_key).auto_paging_iter()):
            if count >= 1000:
                raise ValueError("Refund reconciliation requires ops")
            state["refunds"].append(_stripe_dict(refund))
    return state


@_private_stripe_reads()
def reconcile_payment_event(event: dict[str, Any], settings: Settings) -> bool:
    """Reconcile external card changes before the generic credit webhook handlers."""
    obj = event["data"]["object"]
    payment_intent = obj.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    charge = None
    if not payment_intent and obj.get("charge"):
        charge = _stripe_dict(stripe.Charge.retrieve(obj["charge"], api_key=settings.stripe_secret_key))
        payment_intent = charge.get("payment_intent")
    if not payment_intent:
        return False
    if event["type"] in {"refund.failed", "refund.updated", "refund.created", "charge.refund.updated"}:
        refund = {**obj, "payment_intent": payment_intent,
                  "status": "failed" if event["type"] == "refund.failed" else obj.get("status")}
        if refund["status"] in {"failed", "canceled"}:
            reopened = command("refund_failed", refund=refund)
            if reopened:
                alert_failed_refund(reopened, settings)
            return bool(reopened or command("payment", payment_intent=payment_intent)) or (obj.get("metadata") or {}).get("kind") == "reserved_tokens_2027"
        if refund["status"] not in {"pending", "succeeded", "requires_action"}:
            return False
        # Use the signed event itself: a successful early refund must survive
        # even if a subsequent charge read is stale or temporarily unavailable.
        state = {"amount_refunded": refund["amount"], "refunds": [refund]}
    elif event["type"].startswith("charge.dispute."):
        state = {"disputed": True}
    else:
        if event["type"] == "charge.refunded":
            charge = obj
        state = payment_state(payment_intent, charge, settings)
    position = command("reconcile_payment", payment_intent=payment_intent, payment_state=state)
    # Unmapped evidence is still durable. Only intercept generic billing when
    # this is a known NYTE payment; unrelated payments retain their old handler.
    return bool(position) or (obj.get("metadata") or {}).get("kind") == "reserved_tokens_2027"


def alert_failed_refund(fill: dict[str, Any], settings: Settings) -> None:
    alert_ops(settings, "NYTE resale refund failed after issuance",
              f"Fill {fill['id']}, Stripe refund {fill['stripe_refund_id']} needs ops reconciliation. "
              "Automatic re-issuance is blocked. Inspect python -m scripts.retry_nyte_resale_refunds --needs-ops.")


def _stripe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else value._to_dict_recursive()


@_private_stripe_reads()
def acknowledge_full_refund(session_id: str, refund_id: str, settings: Settings) -> None:
    """Ops-only CLI reconciliation, after issuing the original-card refund."""
    p = command("position", position=session_id)
    refund = _stripe_dict(stripe.Refund.retrieve(refund_id, api_key=settings.stripe_secret_key))
    if not p or refund.get("status") != "succeeded" or refund.get("payment_intent") != p.get("payment_intent") or refund.get("amount") != p["lots"] * p["per_lot_cents"]:
        raise ValueError("Stripe refund does not match a successful full original-payment refund")
    command("refund_complete", position=session_id, stripe_refund_id=refund_id)
