"""Finite PayPal Transaction Search scans and direct outstanding-object reads."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from trusted_router.config import Settings
from trusted_router.provider_trust_history import provider_scan
from trusted_router.services.paypal_billing import (
    _access_token,
    _paypal_base_url,
    _paypal_capture_payload,
)
from trusted_router.services.paypal_trust import paypal_adverse_events
from trusted_router.services.provider_trust import timestamp
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, TrustEvent
from trusted_router.stripe_trust_history import StripeTrustScan
from trusted_router.trust_reconciliation import OutstandingAdverse
from trusted_router.trust_tiers import payment_or_grant_event

CONSISTENCY_DELAY_SECONDS = 10_800
WINDOW = timedelta(days=31)


class PayPalHistoryAPI(Protocol):
    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]: ...


class PayPalHistoryClient:
    """Uses the same sandbox/live selection and OAuth credentials as checkout."""
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                _paypal_base_url(self.settings) + path, params=params,
                headers={"Authorization": f"Bearer {_access_token(self.settings)}"},
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict):
            raise ValueError("PayPal history response must be an object")
        return result


def history_start(first_capture: datetime, *, now: datetime) -> datetime:
    # Calendar years, including Feb 29, rather than a 1095-day approximation.
    try:
        oldest = now.replace(year=now.year - 3)
    except ValueError:
        oldest = now.replace(year=now.year - 3, day=28)
    if first_capture.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("PayPal history timestamps must be timezone-aware")
    return max(first_capture, oldest)


def transaction_windows(start: datetime, end: datetime, *, now: datetime) -> Iterator[tuple[datetime, datetime]]:
    if start < history_start(start, now=now):
        raise ValueError("PayPal Transaction Search is limited to three years")
    if start >= end or end > now - timedelta(seconds=CONSISTENCY_DELAY_SECONDS):
        raise ValueError("PayPal range must end outside the three-hour consistency delay")
    while start < end:
        stop = min(start + WINDOW, end)
        yield start, stop
        start = stop


def paypal_payment(capture: Mapping[str, Any], *, recorded_at: datetime) -> TrustEvent:
    parsed = _paypal_capture_payload(capture, order_id="")
    if parsed["status"] not in {"COMPLETED", "REFUNDED", "PARTIALLY_REFUNDED", "REVERSED"}:
        raise ValueError("PayPal canonical capture is not a completed payment")
    return payment_or_grant_event(
        parsed["workspace_id"], f"paypal_capture:{parsed['capture_id']}", parsed["amount_microdollars"],
        CreditProvenance("capture", "paypal", parsed["capture_id"], timestamp(capture["create_time"])),
        recorded_at=recorded_at, payment_amount_microdollars=parsed["charge_amount_microdollars"],
        currency="USD",
    )


def scan_paypal_created_range(
    client: PayPalHistoryAPI, *, account_id: str, start: datetime, end: datetime, recorded_at: datetime,
) -> StripeTrustScan:
    if not account_id:
        raise ValueError("PayPal history requires the merchant account id")
    rows: dict[str, dict[str, Any]] = {}
    unmatched: list[str] = []
    for begin, stop in transaction_windows(start, end, now=recorded_at):
        page = 1
        while True:
            body = client.get("/v1/reporting/transactions", {
                "start_date": begin.isoformat(), "end_date": stop.isoformat(),
                "fields": "all", "page_size": 500, "page": page,
                "balance_affecting_records_only": "N",
            })
            details = body.get("transaction_details")
            pages = body.get("total_pages")
            if not isinstance(details, list) or not isinstance(pages, int) or pages < 1:
                raise ValueError("PayPal Transaction Search pagination is incomplete")
            for row in details:
                info = row["transaction_info"]
                if info.get("paypal_account_id") != account_id:
                    raise ValueError("PayPal Transaction Search merchant account mismatch")
                created = timestamp(info["transaction_initiation_date"])
                if not start <= created < end:
                    continue  # API end timestamps are inclusive; our proof is half-open.
                key = str(info["transaction_id"])
                previous = rows.get(key)
                if previous is not None and previous != info:
                    unmatched.append(f"conflicting-transaction:{key}")
                rows[key] = info
            if page >= pages:
                break
            page += 1
    payments: dict[str, TrustEvent] = {}
    adverse: list[AdverseTrustEvent] = []

    def payment(ref: str) -> None:
        if ref not in payments:
            payments[ref] = paypal_payment(client.get(f"/v2/payments/captures/{ref}"), recorded_at=recorded_at)

    for ref, info in rows.items():
        code = str(info.get("transaction_event_code") or "")
        try:
            if code.startswith("T00"):
                # Capture retrieval is also the attribution authority; a foreign
                # transaction cannot silently disappear from the completion proof.
                payment(ref)
                continue
            if code == "T1107":
                resource = client.get(f"/v2/payments/refunds/{ref}")
                event_code = "PAYMENT.CAPTURE.REFUNDED"
            elif code == "T1106":
                original = str(info.get("paypal_reference_id") or "")
                resource = client.get(f"/v2/payments/captures/{original}")
                event_code = "PAYMENT.CAPTURE.REVERSED"
            elif code.startswith("T12"):
                # Transaction search references the payment, not a dispute id.
                # Enumerate disputes for that transaction and retain every id.
                original = str(info.get("paypal_reference_id") or "")
                disputes = client.get("/v1/customer/disputes", {"disputed_transaction_id": original})
                if any(link.get("rel") == "next" for link in disputes.get("links", [])):
                    raise ValueError("PayPal dispute pagination is incomplete")
                items = disputes.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("PayPal chargeback has no canonical dispute")
                for item in items:
                    resource = client.get(f"/v1/customer/disputes/{item['dispute_id']}")
                    events = paypal_adverse_events({"event_type": "CUSTOMER.DISPUTE.UPDATED", "resource": resource,
                                                   "create_time": resource.get("update_time") or info["transaction_updated_date"]})
                    for event in events:
                        payment(event.original_payment_ref)
                        adverse.append(event)
                continue
            elif code.startswith(("T11", "T01", "T15")):
                raise ValueError("Unsupported PayPal adjustment code")
            else:
                continue  # Non-payment account transfers/fees cannot qualify.
            events = paypal_adverse_events({"event_type": event_code, "resource": resource,
                                           "create_time": resource.get("update_time") or info["transaction_updated_date"]})
            for event in events:
                payment(event.original_payment_ref)
                adverse.append(event)
        except Exception:
            unmatched.append(ref)
    # A newly opened dispute need not have a balance-affecting transaction.
    # Supplement Transaction Search with the provider's created-time dispute
    # list; otherwise a lost CREATED webhook could evade reconciliation.
    params: dict[str, Any] = {"start_time": start.isoformat(), "page_size": 50}
    seen_tokens: set[str] = set()
    while True:
        body = client.get("/v1/customer/disputes", params)
        items = body.get("items")
        if not isinstance(items, list):
            raise ValueError("PayPal dispute enumeration is incomplete")
        for item in items:
            ref = str(item.get("dispute_id") or "")
            try:
                resource = client.get(f"/v1/customer/disputes/{ref}")
                if not start <= timestamp(resource["create_time"]) < end:
                    continue
                for event in paypal_adverse_events({
                    "event_type": "CUSTOMER.DISPUTE.UPDATED", "resource": resource,
                    "create_time": resource.get("update_time"),
                }):
                    payment(event.original_payment_ref)
                    adverse.append(event)
            except Exception:
                unmatched.append(ref or "dispute:missing_id")
        next_links = [link for link in body.get("links", []) if link.get("rel") == "next"]
        if not next_links:
            break
        if len(next_links) != 1:
            raise ValueError("PayPal dispute pagination is ambiguous")
        query = parse_qs(urlparse(next_links[0]["href"]).query)
        tokens = query.get("next_page_token", [])
        if len(tokens) != 1 or tokens[0] in seen_tokens:
            raise ValueError("PayPal dispute pagination did not advance")
        seen_tokens.add(tokens[0])
        params = {"next_page_token": tokens[0], "page_size": 50}
    unique = {(event.provider, event.adverse_ref, event.provider_ordering_watermark): event for event in adverse}
    return provider_scan(payments.values(), unique.values(), recorded_at=recorded_at, unmatched_ids=unmatched)


def refetch_paypal_adverse(
    client: PayPalHistoryAPI, row: OutstandingAdverse, now: datetime,
) -> tuple[AdverseTrustEvent, OutstandingAdverse]:
    subtype, ref = row.adverse_ref.split(":", 1)
    path = f"/v2/payments/refunds/{ref}" if row.kind == "refund" else f"/v1/customer/disputes/{ref}"
    resource = client.get(path)
    code = "PAYMENT.CAPTURE.REFUNDED" if subtype == "refund" else "CUSTOMER.DISPUTE.UPDATED"
    event, = paypal_adverse_events({"event_type": code, "resource": resource,
                                   "create_time": resource.get("update_time")})
    if event.original_payment_ref != row.original_payment_ref or event.adverse_ref != row.adverse_ref:
        raise ValueError("PayPal outstanding object identity changed")
    return event, OutstandingAdverse(
        event.provider, event.kind, event.adverse_ref, event.original_payment_ref,
        event.lifecycle_status, event.occurred_at,
    )
