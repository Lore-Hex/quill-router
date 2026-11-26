"""Consume authenticated billing events, never customer errors or timers."""

from __future__ import annotations

import base64
import re
import time
from decimal import Decimal
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trusted_router.config import Settings
from trusted_router.store_protocol import Store

_DOMAIN = b"tr.managed-key-exhaustion/1\0"
_FINAL = {
    "fal": "auto_refill_closed",
    "minimax": "subscription_finally_ended",
}


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    payload: str = Field(max_length=2048)
    signature: str = Field(max_length=88)


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    audience: str
    provider: Literal["fal", "minimax"]
    generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: int
    primary: str
    # Exact decimal strings avoid treating a small remaining credit as zero.
    # fal: remaining credit; MiniMax: remaining active entitlements.
    primary_remaining: str = Field(max_length=48, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
    standby_available_credit: str = Field(max_length=48, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")


def accept(settings: Settings, store: Store, delivery: Delivery) -> tuple[str, str]:
    # The dedicated accounting verifier, NOT a gateway caller, attests facts for
    # the exact ordered accounts. Verify the original bytes before decoding them.
    if not settings.managed_key_receipt_audience or not settings.managed_key_receipt_public_key:
        raise ValueError("not enrolled")
    try:
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(settings.managed_key_receipt_public_key, validate=True)
        )
        raw = base64.b64decode(delivery.payload, validate=True)
        key.verify(base64.b64decode(delivery.signature, validate=True), _DOMAIN + raw)
        e = _Evidence.model_validate_json(raw)
    except (ValueError, InvalidSignature, ValidationError):
        raise ValueError("invalid funding evidence") from None
    if (
        e.audience != settings.managed_key_receipt_audience
        or e.generation != settings.managed_key_generations.get(e.provider)
        or e.primary != _FINAL[e.provider]
        or Decimal(e.primary_remaining) != 0
        or Decimal(e.standby_available_credit) <= 0
        or not 0 <= int(time.time()) - e.observed_at <= 120
    ):
        raise ValueError("ineligible funding evidence")
    store.record_managed_key_promotion(e.provider, e.generation)
    return e.provider, e.generation


def decisions(settings: Settings, store: Store, data: dict[str, Any]) -> dict[str, str]:
    routes = [data, *(data.get("route_candidates") or [])]
    providers = {
        r.get("provider")
        for r in routes
        if isinstance(r, dict) and r.get("usage_type") == "Credits"
    }
    result = {}
    for provider, generation in settings.managed_key_generations.items():
        if provider not in providers or provider not in _FINAL:
            continue
        if not re.fullmatch("[0-9a-f]{64}", generation):
            result[provider] = "unavailable"
            continue
        try:
            promoted = store.managed_key_promoted(provider, generation)
        except Exception:  # Do not reselect primary when the irreversible marker cannot be read.
            result[provider] = "unavailable"
        else:
            result[provider] = generation if promoted else "primary:" + generation
    return result
