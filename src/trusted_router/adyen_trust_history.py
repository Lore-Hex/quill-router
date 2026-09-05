"""Adyen Payment Accounting Report source, including modification lineage.

Reports must cover the complete requested interval. The caller supplies the
export's account/environment and coverage, never a local-ledger reconstruction.
"""
from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trusted_router.provider_trust_history import provider_scan
from trusted_router.services.adyen_billing import _parse_checkout_reference
from trusted_router.services.adyen_trust import adyen_adverse_events
from trusted_router.services.provider_trust import timestamp
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, TrustEvent
from trusted_router.stripe_trust_history import StripeTrustScan
from trusted_router.trust_reconciliation import OutstandingAdverse
from trusted_router.trust_tiers import payment_or_grant_event

RECORD_CODES = {
    "Refunded": "REFUND", "RefundFailed": "REFUND_FAILED",
    "RefundedReversed": "REFUNDED_REVERSED", "Cancelled": "CANCELLATION",
    "CancelOrRefund": "CANCEL_OR_REFUND", "CaptureFailed": "CAPTURE_FAILED",
    "CaptureReversed": "CAPTURE_REVERSED", "TechnicalCancel": "TECHNICAL_CANCEL",
    "NotificationOfFraud": "NOTIFICATION_OF_FRAUD",
    "NotificationOfChargeback": "NOTIFICATION_OF_CHARGEBACK",
    "Chargeback": "CHARGEBACK", "ChargebackReversed": "CHARGEBACK_REVERSED",
    "SecondChargeback": "SECOND_CHARGEBACK",
    "SentForRefund": "REFUND",
}
NON_ADVERSE_RECORDS = frozenset({"Authorised", "Received", "SentForSettle", "Settled", "Refused", "Error", "Expired", "SettledReversed"})


def read_payment_accounting_report(path: Path) -> tuple[dict[str, str], ...]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"Merchant Account", "Psp Reference", "Merchant Reference", "Record Type",
                    "Booking Date", "TimeZone", "Main Currency", "Main Amount", "Modification Psp Reference"}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError("Adyen Payment Accounting Report columns are incomplete")
        return tuple(dict(row) for row in reader)


def _booking(row: Mapping[str, str]) -> datetime:
    raw = datetime.fromisoformat(row["Booking Date"])
    if raw.utcoffset() is None:
        raw = raw.replace(tzinfo=ZoneInfo(row["TimeZone"]))
    return raw.astimezone(UTC)


class AdyenAccountingSource:
    def __init__(
        self, rows: Iterable[Mapping[str, str]], *, account_id: str, environment: str,
        covered_from: datetime, covered_through: datetime, reference_key: str,
    ) -> None:
        if environment not in {"test", "live"} or not account_id:
            raise ValueError("Adyen report account and test/live environment are required")
        if covered_from.utcoffset() is None or covered_through.utcoffset() is None or covered_from >= covered_through:
            raise ValueError("Adyen report coverage must be a finite aware interval")
        self.rows = tuple(dict(row) for row in rows)
        self.account_id = account_id
        self.environment = environment
        self.covered_from = covered_from
        self.covered_through = covered_through
        self.reference_key = reference_key
        if any(row["Merchant Account"] != account_id for row in self.rows):
            raise ValueError("Adyen report merchant mismatch")

    def _payment(self, row: Mapping[str, str], now: datetime) -> TrustEvent:
        ref = _parse_checkout_reference(row["Merchant Reference"], reference_key=self.reference_key)
        amount = Decimal(row["Main Amount"]) * 1_000_000
        if row["Main Currency"] != "USD" or amount != ref.charge_amount_cents * 10_000:
            raise ValueError("Adyen report authorisation amount/currency mismatch")
        return payment_or_grant_event(
            ref.workspace_id, f"adyen_checkout:{row['Merchant Reference']}", ref.credit_amount_cents * 10_000,
            CreditProvenance("authorisation", "adyen", row["Psp Reference"], _booking(row)),
            recorded_at=now, payment_amount_microdollars=int(amount), currency="USD",
        )

    def _event(self, row: Mapping[str, str]) -> AdverseTrustEvent:
        code = RECORD_CODES[row["Record Type"]]
        # PAR separates the original authorisation from each modification id.
        # Never treat Modification Psp Reference as the payment identity.
        reference = row["Modification Psp Reference"]
        if not reference or not row["Psp Reference"]:
            raise ValueError("Adyen report modification lineage is missing")
        amount = abs(Decimal(row["Main Amount"])) * 100
        if amount != amount.to_integral_value():
            raise ValueError("Adyen report amount is not integral cents")
        event, = adyen_adverse_events({
            "eventCode": code, "pspReference": reference, "originalReference": row["Psp Reference"],
            "eventDate": _booking(row).isoformat(), "success": "true",
            "amount": {"currency": row["Main Currency"], "value": int(amount)},
        })
        if row["Record Type"] == "SentForRefund":
            from dataclasses import replace
            event = replace(event, lifecycle_status="pending",
                            provider_ordering_watermark=event.provider_ordering_watermark.rsplit(":", 1)[0] + ":pending")
        return event

    def scan(self, start: datetime, end: datetime, recorded_at: datetime) -> StripeTrustScan:
        if not self.covered_from <= start < end <= self.covered_through or self.covered_through > recorded_at:
            raise ValueError("Adyen report does not cover the requested closed interval")
        payments: dict[str, TrustEvent] = {}
        unmatched: list[str] = []
        for row in self.rows:
            if row["Record Type"] == "Authorised":
                try:
                    event = self._payment(row, recorded_at)
                    existing = payments.get(row["Psp Reference"])
                    if existing is not None and existing != event:
                        raise ValueError("conflicting authorisation")
                    payments[row["Psp Reference"]] = event
                except Exception:
                    unmatched.append(row["Psp Reference"])
        adverse: list[AdverseTrustEvent] = []
        for row in self.rows:
            if not start <= _booking(row) < end:
                continue
            record = row["Record Type"]
            if record in RECORD_CODES:
                try:
                    adverse.append(self._event(row))
                except Exception:
                    unmatched.append(row["Modification Psp Reference"] or row["Psp Reference"])
            elif record not in NON_ADVERSE_RECORDS:
                unmatched.append(f"unsupported-record:{record}:{row['Psp Reference']}")
        needed = {event.original_payment_ref for event in adverse}
        selected = [event for ref, event in payments.items() if ref in needed or start <= event.occurred_at < end]
        return provider_scan(selected, adverse, recorded_at=recorded_at, unmatched_ids=unmatched)

    def refetch(self, row: OutstandingAdverse, now: datetime) -> tuple[AdverseTrustEvent, OutstandingAdverse]:
        if self.covered_through < now:
            raise ValueError("Adyen outstanding re-fetch needs reports closed through this pass")
        events = [self._event(item) for item in self.rows if item["Record Type"] in RECORD_CODES
                  and item["Modification Psp Reference"] == row.adverse_ref.split(":", 1)[1]]
        events = [event for event in events if event.adverse_ref == row.adverse_ref]
        if not events:
            raise ValueError("Adyen outstanding modification absent from complete report")
        event = max(events, key=lambda item: item.provider_ordering_watermark)
        if event.original_payment_ref != row.original_payment_ref:
            raise ValueError("Adyen outstanding authorisation changed")
        return event, OutstandingAdverse(event.provider, event.kind, event.adverse_ref,
                                        event.original_payment_ref, event.lifecycle_status,
                                        timestamp(event.occurred_at.isoformat()))
