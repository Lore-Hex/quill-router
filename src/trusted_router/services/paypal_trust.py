"""PayPal capture refunds, reversals and disputes on the existing webhook."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from trusted_router.services.provider_trust import invalid, observation, timestamp, usd_micro
from trusted_router.storage_models import AdverseTrustEvent

PAYPAL_ADVERSE_EVENTS = frozenset({
    "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED",
    "PAYMENT.REFUND.PENDING", "PAYMENT.REFUND.COMPLETED", "PAYMENT.REFUND.FAILED",
    "CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED",
})


def _capture_reference(resource: Mapping[str, Any]) -> str:
    related = resource.get("supplementary_data", {}).get("related_ids", {})
    capture = str(related.get("capture_id") or "")
    for link in resource.get("links", []):
        parsed = urlparse(str(link.get("href") or ""))
        path = parsed.path.split("/")
        if link.get("rel") == "up" and len(path) == 5 and path[1:4] == ["v2", "payments", "captures"]:
            linked = path[-1]
            if capture and capture != linked:
                return invalid("PayPal refund capture references disagree")
            capture = linked
    return capture


def paypal_adverse_events(event: Mapping[str, Any]) -> tuple[AdverseTrustEvent, ...]:
    code = str(event.get("event_type") or "")
    if code not in PAYPAL_ADVERSE_EVENTS:
        return ()
    resource = event.get("resource")
    if not isinstance(resource, Mapping):
        return invalid("PayPal adverse resource is missing")
    created = timestamp(resource.get("create_time") or event.get("create_time"))
    updated = timestamp(resource.get("update_time") or event.get("create_time"))
    if code.startswith("CUSTOMER.DISPUTE."):
        reference = str(resource.get("dispute_id") or "")
        transactions = resource.get("disputed_transactions", [])
        if len(transactions) != 1:
            return invalid("PayPal dispute requires one original capture")
        payment = str(transactions[0].get("seller_transaction_id") or "")
        raw_status = str(resource.get("status") or "")
        if raw_status == "RESOLVED":
            outcome = resource.get("dispute_outcome", {}).get("outcome_code")
            statuses = {"RESOLVED_SELLER_FAVOUR": "won", "RESOLVED_BUYER_FAVOUR": "lost",
                        "RESOLVED_WITH_PAYOUT": "lost", "CANCELED_BY_BUYER": "won",
                        "ACCEPTED": "lost", "DENIED": "won"}
            if outcome not in statuses:
                return invalid("Unsupported PayPal dispute outcome")
            status = statuses[outcome]
        elif raw_status in {"OPEN", "WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE", "UNDER_REVIEW"}:
            status = "succeeded"
        else:
            return invalid("Unsupported PayPal dispute status")
        kind, subtype = "dispute", "dispute"
        amount = usd_micro(resource.get("dispute_amount"))
    else:
        reference = str(resource.get("id") or "")
        amount = usd_micro(resource.get("amount"))
        if code == "PAYMENT.CAPTURE.REVERSED":
            payment, kind, subtype, status = reference, "dispute", "reversal", "lost"
        else:
            payment = _capture_reference(resource)
            kind, subtype = "refund", "refund"
            raw_status = str(resource.get("status") or code.rsplit(".", 1)[-1])
            statuses = {"COMPLETED": "succeeded", "REFUNDED": "succeeded", "PENDING": "pending",
                        "FAILED": "failed", "CANCELLED": "failed", "CANCELED": "failed"}
            if raw_status not in statuses:
                return invalid("Unsupported PayPal refund status")
            status = statuses[raw_status]
    return (observation(provider="paypal", reference=reference, payment=payment, kind=kind,
                        subtype=subtype, status=status, amount=amount, created=created, updated=updated),)
