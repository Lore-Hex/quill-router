"""Adyen modifications resolve originalReference to authorisation provenance."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trusted_router.services.provider_trust import invalid, observation, timestamp, usd_micro
from trusted_router.storage_models import AdverseTrustEvent

# Full-principal claims use the existing dispute graph and recovery formula.
# A fraud/chargeback notification latches without claiming until charged back.
ADYEN_ADVERSE_CODES = {
    "REFUND": ("refund", "refund", "succeeded"),
    "REFUND_FAILED": ("refund", "refund", "reversed"),
    "REFUNDED_REVERSED": ("dispute", "refund_reversal", "lost"),
    "CANCEL_OR_REFUND": ("dispute", "cancellation", "lost"),
    "CANCELLATION": ("dispute", "cancellation", "lost"),
    "TECHNICAL_CANCEL": ("dispute", "cancellation", "lost"),
    "CAPTURE_FAILED": ("dispute", "capture_failure", "lost"),
    "CAPTURE_REVERSED": ("dispute", "capture_reversal", "lost"),
    "NOTIFICATION_OF_FRAUD": ("dispute", "fraud", "pending"),
    "NOTIFICATION_OF_CHARGEBACK": ("dispute", "chargeback", "pending"),
    "CHARGEBACK": ("dispute", "chargeback", "succeeded"),
    "CHARGEBACK_REVERSED": ("dispute", "chargeback", "won"),
    "SECOND_CHARGEBACK": ("dispute", "second_chargeback", "lost"),
}


def adyen_adverse_events(item: Mapping[str, Any]) -> tuple[AdverseTrustEvent, ...]:
    code = str(item.get("eventCode") or "")
    if code not in ADYEN_ADVERSE_CODES:
        return ()
    reference = str(item.get("pspReference") or "")
    original = str(item.get("originalReference") or "")
    # Older signed deliveries with no original reference are durable unmatched
    # work, never an inferred workspace or a silently acknowledged adverse fact.
    payment = original or f"unresolved:{reference}"
    created = timestamp(item.get("eventDate") or "1970-01-01T00:00:00Z")
    kind, subtype, status = ADYEN_ADVERSE_CODES[code]
    success = str(item.get("success")).lower()
    if success not in {"true", "false"}:
        return invalid("Invalid Adyen adverse success")
    if success == "false":
        if kind == "refund" and code != "REFUND_FAILED":
            status = "failed"
        elif code not in {"CAPTURE_FAILED", "TECHNICAL_CANCEL"}:
            # An unsuccessful cancellation/chargeback is not an active claim.
            return ()
    return (observation(provider="adyen", reference=reference, payment=payment, kind=kind,
                        subtype=subtype, status=status,
                        amount=usd_micro(item.get("amount"), minor_units=True),
                        created=created, updated=created),)
