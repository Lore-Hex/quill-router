"""Canonical provider observations; money is applied only by the trust writer."""
from __future__ import annotations

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
        if isinstance(raw, bool):
            raise ValueError("boolean amount")
        value = Decimal(str(raw)) * (10_000 if minor_units else 1_000_000)
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            raise ValueError("invalid amount")
        return int(value)
    except (KeyError, ValueError, InvalidOperation):
        return invalid("Invalid provider adverse amount")


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
        provider_ordering_watermark=f"{updated.isoformat(timespec='microseconds')}:{status}",
        payload="",
    )
