"""Reconcile fully refunded captures that never created local credit.

Original inbox events remain available to the credit transaction. A delayed
COMPLETED callback must still drain them and recover the refunded principal.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.paypal_trust_history import PayPalHistoryAPI, PayPalHistoryClient
from trusted_router.services.paypal_billing import _paypal_capture_payload
from trusted_router.services.paypal_trust import paypal_adverse_events
from trusted_router.storage_models import TrustInboxRow
from trusted_router.trust_tiers import adverse_event_from_payload

log = logging.getLogger(__name__)
RESOLUTION_KIND = "trust_inbox_resolution"
MAX_CAPTURES_PER_RUN = 3
MAX_ROWS_PER_CAPTURE = 100


@dataclass(frozen=True)
class RefundedCaptureProof:
    capture_id: str
    workspace_id: str
    amount_micro: int
    refund_ref: str
    verified_at: datetime

    def receipt(self, row: TrustInboxRow) -> dict[str, Any]:
        event = adverse_event_from_payload(row.payload)
        if (
            row.provider != "paypal" or event.provider != "paypal"
            or event.original_payment_ref != self.capture_id
            or event.amount_micro > self.amount_micro
            or event.occurred_at > self.verified_at
        ):
            raise ValueError("refund proof does not cover this observation")
        return {
            "version": 1, "resolution": "verified_refunded_uncredited",
            "provider": "paypal", "capture_id": self.capture_id,
            "workspace_id": self.workspace_id, "amount_micro": self.amount_micro,
            "refund_ref": self.refund_ref, "capture_status": "REFUNDED",
            "verified_at": self.verified_at.isoformat(),
            "inbox_payload_sha256": hashlib.sha256(row.payload.encode()).hexdigest(),
            "credited_micro": 0,
        }


def resolution_key(row: TrustInboxRow) -> str:
    return f"{row.provider}:{row.adverse_ref}"


def verify_refunded_capture(
    client: PayPalHistoryAPI, rows: tuple[TrustInboxRow, ...], *, now: datetime,
) -> RefundedCaptureProof | None:
    """Two canonical GETs; never trust webhook amounts as capture attribution."""
    if not rows or len(rows) > MAX_ROWS_PER_CAPTURE:
        return None
    events = tuple(adverse_event_from_payload(row.payload) for row in rows)
    capture_id = events[0].original_payment_ref
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", capture_id):
        raise ValueError("invalid capture reference")
    if any(event.provider != "paypal" or event.original_payment_ref != capture_id for event in events):
        raise ValueError("mixed capture observations")
    capture = client.get(f"/v2/payments/captures/{capture_id}")
    if capture.get("id") != capture_id:
        raise ValueError("canonical capture identity mismatch")
    if capture.get("status") != "REFUNDED":
        return None
    parsed = _paypal_capture_payload(capture, order_id="")
    amount = int(parsed["charge_amount_microdollars"])
    # A complete successful refund must already be durably in the inbox.
    # Partial refunds and a dispute alone are not proof of zero exposure.
    refunds = [event for event in events if event.kind == "refund"
               and event.lifecycle_status == "succeeded" and event.amount_micro == amount
               and event.adverse_ref.startswith("refund:")]
    if len({event.adverse_ref for event in refunds}) != 1:
        return None
    refund_ref = refunds[0].adverse_ref
    refund_id = refund_ref.removeprefix("refund:")
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", refund_id):
        raise ValueError("invalid refund reference")
    resource = client.get(f"/v2/payments/refunds/{refund_id}")
    if resource.get("id") != refund_id:
        raise ValueError("canonical refund identity mismatch")
    canonical, = paypal_adverse_events({
        "event_type": "PAYMENT.REFUND.COMPLETED", "resource": resource,
        "create_time": resource.get("update_time"),
    })
    if (canonical.original_payment_ref != capture_id or canonical.adverse_ref != refund_ref
            or canonical.lifecycle_status != "succeeded" or canonical.amount_micro != amount):
        raise ValueError("canonical refund does not cover the capture")
    proof = RefundedCaptureProof(capture_id, str(parsed["workspace_id"]), amount, refund_ref, now)
    for row in rows:
        proof.receipt(row)
    return proof


def reconcile_uncredited_paypal_inbox(
    store: Any, settings: Any, *, now: datetime | None = None,
    client: PayPalHistoryAPI | None = None,
) -> int:
    from trusted_router.storage_trust_inbox_resolution import (
        record_refunded_uncredited,
        retained_refund_row,
    )

    if not getattr(settings, "paypal_client_id", None) or not getattr(settings, "paypal_client_secret", None):
        return 0
    observed_at = now or datetime.now(UTC)
    groups: dict[str, list[TrustInboxRow]] = defaultdict(list)
    for row in store.list_stale_trust_inbox(older_than=observed_at - timedelta(hours=3)):
        if row.provider == "paypal":
            event = adverse_event_from_payload(row.payload)
            groups[event.original_payment_ref].append(row)
    resolved = 0
    api = client or PayPalHistoryClient(settings)
    candidates = list(groups.items())
    # Pending/unknown captures must not permanently starve later full refunds.
    offset = (int(observed_at.timestamp()) // 900 * MAX_CAPTURES_PER_RUN) % max(1, len(candidates))
    batch = (candidates[offset:] + candidates[:offset])[:MAX_CAPTURES_PER_RUN]
    for capture_id, group in batch:
        try:
            retained = retained_refund_row(store, capture_id)
            if retained is not None and retained.adverse_ref not in {row.adverse_ref for row in group}:
                group.append(retained)
            proof = verify_refunded_capture(api, tuple(group), now=observed_at)
            if proof is not None:
                count = record_refunded_uncredited(store, proof, tuple(group))
                resolved += count
                if count:
                    log.info("trust.inbox_reconciled provider=paypal payment_ref=%s "
                             "workspace_id=%s observations=%d credited_micro=0",
                             capture_id, proof.workspace_id, count)
        except Exception as exc:
            # Leave every original event unresolved and alertable. Do not log
            # provider bodies/credentials from an HTTP exception.
            log.warning("trust.inbox_verify_failed provider=paypal payment_ref=%s error_type=%s",
                        capture_id, type(exc).__name__)
    return resolved
