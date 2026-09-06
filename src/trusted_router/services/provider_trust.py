"""Canonical provider observations; money is applied only by the trust writer."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from trusted_router.errors import api_error
from trusted_router.storage_models import AdverseTrustEvent
from trusted_router.types import ErrorType


def invalid(message: str) -> Any:
    raise api_error(400, message, ErrorType.BAD_REQUEST)


def timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError("timezone required")
        return result.astimezone(UTC)
    except (ValueError, TypeError):
        return invalid("Invalid provider trust timestamp")


def usd_micro(amount: Any, *, minor_units: bool = False) -> int:
    if not isinstance(amount, Mapping):
        return invalid("Provider adverse amount is missing")
    currency = amount.get("currency_code", amount.get("currency"))
    if currency != "USD":
        return invalid("Provider adverse currency must be USD")
    try:
        raw = amount["value"]
        if isinstance(raw, bool) or (minor_units and not isinstance(raw, int)):
            raise ValueError("minor-unit amounts require integers")
        value = Decimal(str(raw)) * (10_000 if minor_units else 1_000_000)
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            raise ValueError("invalid amount")
        return int(value)
    except (KeyError, ValueError, InvalidOperation):
        return invalid("Invalid provider adverse amount")


def ordering_watermark(updated: datetime, status: str) -> str:
    # Provider clocks may have only second precision. Use the transition
    # graph's topological order for ties: a reversal cannot sort before the
    # successful refund it reverses. The writer still rejects illegal edges.
    rank = {"pending": 0, "succeeded": 1, "failed": 1, "reversed": 2,
            "won": 2, "lost": 2, "closed": 3, "terminal_by_horizon": 4}[status]
    return f"{updated.isoformat(timespec='microseconds')}:{rank}:{status}"


def observation(
    *, provider: str, reference: str, payment: str, kind: str,
    subtype: str, status: str, amount: int, created: datetime, updated: datetime,
) -> AdverseTrustEvent:
    if not reference or not payment:
        return invalid("Provider adverse and original payment references are required")
    # Object namespaces prevent a capture reversal, refund and dispute sharing
    # a raw provider id from colliding in the inherited (provider, ref) inbox.
    adverse_ref = f"{subtype}:{reference}"
    return AdverseTrustEvent(
        event_id=f"trust:{provider}:{adverse_ref}", provider=provider, kind=kind,
        adverse_ref=adverse_ref, original_payment_ref=payment,
        amount_micro=amount, provider_subtype=subtype, lifecycle_status=status,
        occurred_at=created,
        provider_ordering_watermark=ordering_watermark(updated, status),
        payload="",
    )


def provider_inbox_key(event: AdverseTrustEvent) -> str:
    """Retain every provider lifecycle observation until its payment arrives.

    The payload retains the canonical adverse id; this is only the inbox key.
    Existing Stripe/x402 inbox identities are unchanged.
    """
    if event.provider not in {"paypal", "adyen"}:
        return event.adverse_ref
    identity = repr((event.kind, event.adverse_ref, event.provider_ordering_watermark,
                     event.lifecycle_status, event.original_payment_ref, event.amount_micro))
    return "observation:" + hashlib.sha256(identity.encode()).hexdigest()
