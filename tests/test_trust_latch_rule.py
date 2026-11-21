"""The adverse-event trust latch fires only on evidence of fraud.

``adverse_event_latches`` is the one rule the Spanner, Postgres and in-memory
writers share. These tests pin it over every refund/dispute observation the
Stripe builders (live webhook and history converter) can produce, over the
whole lifecycle-status domain, and through the inbox payload. PayPal and
Adyen observations are pinned beside their event contract in
``test_provider_trust_slice1b.py``; the per-backend behavior is in
``test_trust_latch_backends.py``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from trusted_router.routes.internal.webhook import _stripe_adverse_events
from trusted_router.storage_models import AdverseTrustEvent
from trusted_router.stripe_trust_history import (
    DISPUTE_EVENT_TYPES,
    REFUND_EVENT_TYPES,
    scan_stripe_responses,
)
from trusted_router.trust_tiers import (
    TRUST_EVENT_LIFECYCLE_STATUSES,
    adverse_event_from_payload,
    adverse_event_latches,
    adverse_event_payload,
)

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)

#: Refund.status values the Stripe SDK documents, the British spelling and
#: ``reversed`` that the builders also map, and a missing status (which the
#: builders read as ``succeeded``).
REFUND_STATUSES: tuple[str | None, ...] = (
    "pending", "requires_action", "succeeded", "failed", "canceled", "cancelled", "reversed", None,
)
#: Refund.reason values the Stripe SDK documents, plus none.
REFUND_REASONS: tuple[str | None, ...] = (
    None, "", "duplicate", "requested_by_customer", "expired_uncaptured_charge", "fraudulent",
)
#: Dispute.status values the Stripe SDK documents, plus ``closed`` and
#: ``charge_refunded`` (older API versions) and a missing status.
DISPUTE_STATUSES: tuple[str | None, ...] = (
    "warning_needs_response", "warning_under_review", "warning_closed",
    "needs_response", "under_review", "won", "lost", "prevented",
    "closed", "charge_refunded", None,
)
#: The inquiry stages: the issuer asked a question, no funds were withdrawn.
INQUIRY_STATUSES = frozenset({"warning_needs_response", "warning_under_review", "warning_closed", "closed"})


def _refund_object(*, status: str | None, reason: str | None, refund_id: str = "re_rule") -> dict[str, Any]:
    refund: dict[str, Any] = {
        "id": refund_id, "object": "refund", "payment_intent": "pi_rule", "amount": 60, "reason": reason,
    }
    if status is not None:
        refund["status"] = status
    return refund


def _stripe_event(event_type: str, obj: dict[str, Any], *, x402: bool = False) -> dict[str, Any]:
    if x402:
        obj = {**obj, "metadata": {"payment_method": "x402"}}
    return {
        "id": "evt_rule", "object": "event", "type": event_type,
        "created": int(NOW.timestamp()), "data": {"object": obj},
    }


def _refund_event(event_type: str, *, status: str | None, reason: str | None, x402: bool = False) -> dict[str, Any]:
    refund = _refund_object(status=status, reason=reason)
    if event_type == "charge.refunded":
        charge = {
            "id": "ch_rule", "object": "charge", "payment_intent": "pi_rule",
            "refunds": {"object": "list", "data": [refund]},
        }
        return _stripe_event(event_type, charge, x402=x402)
    return _stripe_event(event_type, refund, x402=x402)


def _dispute_object(*, status: str | None, reason: str | None) -> dict[str, Any]:
    dispute: dict[str, Any] = {
        "id": "dp_rule", "object": "dispute", "payment_intent": "pi_rule", "amount": 120, "reason": reason,
    }
    if status is not None:
        dispute["status"] = status
    return dispute


def _only(events: tuple[AdverseTrustEvent, ...]) -> AdverseTrustEvent:
    assert len(events) == 1
    return events[0]


@pytest.mark.parametrize("x402", [False, True], ids=["stripe", "x402"])
@pytest.mark.parametrize("event_type", REFUND_EVENT_TYPES)
@pytest.mark.parametrize("status", REFUND_STATUSES)
@pytest.mark.parametrize("reason", REFUND_REASONS)
def test_live_stripe_refund_latches_only_when_given_as_fraudulent(
    x402: bool, event_type: str, status: str | None, reason: str | None,
) -> None:
    event = _only(_stripe_adverse_events(
        _refund_event(event_type, status=status, reason=reason, x402=x402), event_id="evt_rule",
    ))

    assert (event.provider, event.kind) == ("x402" if x402 else "stripe", "refund")
    assert event.refund_reason == (reason or "")
    # The operator's fraud call is the evidence whatever the refund's status:
    # a pending, failed, cancelled or reversed fraudulent refund latches too.
    assert adverse_event_latches(event) is (reason == "fraudulent")


@pytest.mark.parametrize("x402", [False, True], ids=["stripe", "x402"])
@pytest.mark.parametrize("event_type", DISPUTE_EVENT_TYPES)
@pytest.mark.parametrize("status", DISPUTE_STATUSES)
@pytest.mark.parametrize("reason", ["fraudulent", "product_not_received", None])
def test_live_stripe_dispute_latches_only_as_a_chargeback(
    x402: bool, event_type: str, status: str | None, reason: str | None,
) -> None:
    event = _only(_stripe_adverse_events(
        _stripe_event(event_type, _dispute_object(status=status, reason=reason), x402=x402),
        event_id="evt_rule",
    ))

    assert (event.provider, event.kind) == ("x402" if x402 else "stripe", "dispute")
    # The dispute's own reason is not carried: an inquiry about fraud is still
    # an inquiry, and every chargeback latches whatever its reason.
    assert event.refund_reason == ""
    chargeback = event_type == "charge.dispute.funds_reinstated" or status not in INQUIRY_STATUSES
    assert adverse_event_latches(event) is chargeback


def test_charge_refunded_carries_each_refunds_own_reason() -> None:
    charge = {
        "id": "ch_rule", "object": "charge", "payment_intent": "pi_rule",
        "refunds": {"object": "list", "data": [
            _refund_object(status="succeeded", reason="requested_by_customer", refund_id="re_courtesy"),
            _refund_object(status="succeeded", reason="fraudulent", refund_id="re_fraud"),
        ]},
    }
    events = _stripe_adverse_events(_stripe_event("charge.refunded", charge), event_id="evt_rule")

    assert {event.adverse_ref: (event.refund_reason, adverse_event_latches(event)) for event in events} == {
        "re_courtesy": ("requested_by_customer", False),
        "re_fraud": ("fraudulent", True),
    }


def _history_payment_intent() -> dict[str, Any]:
    return {
        "id": "pi_rule", "object": "payment_intent", "status": "succeeded",
        "created": int(NOW.timestamp()) - 86_400, "amount": 120, "amount_received": 120, "currency": "usd",
        "metadata": {
            "workspace_id": "ws_rule", "payment_method": "card",
            "credit_amount_microdollars": "1000000", "processing_fee_cents": "20", "charge_amount_cents": "120",
        },
    }


def _history_event(kind: str, obj: dict[str, Any]) -> AdverseTrustEvent:
    obj = {**obj, "created": int(NOW.timestamp()), "_trust_ordering_watermark": "00000000000000000002:evt_rule"}
    scan = scan_stripe_responses(
        payment_intents=(_history_payment_intent(),),
        refunds=(obj,) if kind == "refund" else (),
        disputes=(obj,) if kind == "dispute" else (),
        recorded_at=NOW,
    )
    return _only(scan.adverse)


@pytest.mark.parametrize("status", REFUND_STATUSES)
@pytest.mark.parametrize("reason", REFUND_REASONS)
def test_history_refund_decides_the_latch_as_the_live_webhook_does(status: str | None, reason: str | None) -> None:
    history = _history_event("refund", _refund_object(status=status, reason=reason))
    live = _only(_stripe_adverse_events(
        _refund_event("refund.updated", status=status, reason=reason), event_id="evt_rule",
    ))

    assert history.refund_reason == live.refund_reason == (reason or "")
    assert history.lifecycle_status == live.lifecycle_status
    assert adverse_event_latches(history) is adverse_event_latches(live)


@pytest.mark.parametrize("status", DISPUTE_STATUSES)
def test_history_dispute_decides_the_latch_as_the_live_webhook_does(status: str | None) -> None:
    obj = _dispute_object(status=status, reason="fraudulent")
    history = _history_event("dispute", obj)
    live = _only(_stripe_adverse_events(_stripe_event("charge.dispute.updated", obj), event_id="evt_rule"))

    assert history.refund_reason == live.refund_reason == ""
    assert adverse_event_latches(history) is adverse_event_latches(live) is (status not in INQUIRY_STATUSES)


def _event(**changes: Any) -> AdverseTrustEvent:
    base = AdverseTrustEvent(
        event_id="evt", provider="stripe", kind="refund", adverse_ref="re_domain",
        original_payment_ref="pi_domain", amount_micro=1, provider_subtype="refund.updated",
        lifecycle_status="succeeded", occurred_at=NOW, provider_ordering_watermark="1", payload="",
    )
    return dataclasses.replace(base, **changes)


@pytest.mark.parametrize("provider", ["stripe", "x402", "paypal", "adyen"])
@pytest.mark.parametrize("status", sorted(TRUST_EVENT_LIFECYCLE_STATUSES))
@pytest.mark.parametrize("reason", ["", "duplicate", "requested_by_customer", "expired_uncaptured_charge", "fraudulent"])
def test_refund_domain_latches_on_a_fraudulent_reason_at_any_status(provider: str, status: str, reason: str) -> None:
    event = _event(provider=provider, lifecycle_status=status, refund_reason=reason)

    assert adverse_event_latches(event) is (reason == "fraudulent")


@pytest.mark.parametrize("reason", ["FRAUDULENT", " fraudulent "])
def test_fraudulent_refund_reason_is_matched_without_case_or_padding(reason: str) -> None:
    assert adverse_event_latches(_event(refund_reason=reason)) is True


@pytest.mark.parametrize("provider", ["stripe", "x402", "paypal"])
@pytest.mark.parametrize("status", sorted(TRUST_EVENT_LIFECYCLE_STATUSES))
def test_dispute_domain_latches_on_withdrawn_or_decided_funds(provider: str, status: str) -> None:
    event = _event(provider=provider, kind="dispute", provider_subtype="charge.dispute.updated", lifecycle_status=status)

    # pending/closed are Stripe inquiry stages; terminal_by_horizon is the
    # reconciler giving up on an open fact, never new evidence; failed and
    # reversed are refund statuses no dispute builder produces.
    assert adverse_event_latches(event) is (status in {"succeeded", "won", "lost"})


@pytest.mark.parametrize("subtype,status,latches", [
    ("fraud", "pending", True),
    ("chargeback", "pending", False),
    ("chargeback", "succeeded", True),
    ("chargeback", "won", True),
    ("chargeback", "terminal_by_horizon", False),
    ("second_chargeback", "lost", True),
    ("capture_reversal", "lost", True),
    ("capture_failure", "lost", False),
    ("cancellation", "lost", False),
    ("refund_reversal", "lost", False),
])
def test_adyen_dispute_subtypes(subtype: str, status: str, latches: bool) -> None:
    event = _event(provider="adyen", kind="dispute", provider_subtype=subtype, lifecycle_status=status)

    assert adverse_event_latches(event) is latches


@pytest.mark.parametrize("reason", REFUND_REASONS)
def test_inbox_payload_round_trips_the_refund_reason(reason: str | None) -> None:
    event = _only(_stripe_adverse_events(
        _refund_event("refund.updated", status="succeeded", reason=reason), event_id="evt_rule",
    ))

    restored = adverse_event_from_payload(adverse_event_payload(event))

    assert restored == event
    assert adverse_event_latches(restored) is (reason == "fraudulent")


def _legacy_payload(event: AdverseTrustEvent) -> str:
    """The inbox payload as written before observations carried a reason."""

    body = json.loads(adverse_event_payload(event))
    del body["refund_reason"]
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


@pytest.mark.parametrize("event_type", REFUND_EVENT_TYPES)
@pytest.mark.parametrize("reason", REFUND_REASONS)
def test_legacy_inbox_refund_reads_its_reason_from_the_raw_stripe_event(event_type: str, reason: str | None) -> None:
    event = _only(_stripe_adverse_events(
        _refund_event(event_type, status="succeeded", reason=reason), event_id="evt_rule",
    ))

    restored = adverse_event_from_payload(_legacy_payload(event))

    assert restored.refund_reason == (reason or "")
    assert adverse_event_latches(restored) is (reason == "fraudulent")


def test_legacy_inbox_charge_refunded_matches_the_refund_by_id() -> None:
    charge = {
        "id": "ch_rule", "object": "charge", "payment_intent": "pi_rule",
        "refunds": {"object": "list", "data": [
            _refund_object(status="succeeded", reason="fraudulent", refund_id="re_fraud"),
            _refund_object(status="succeeded", reason="duplicate", refund_id="re_duplicate"),
        ]},
    }
    events = _stripe_adverse_events(_stripe_event("charge.refunded", charge), event_id="evt_rule")

    assert {
        event.adverse_ref: adverse_event_from_payload(_legacy_payload(event)).refund_reason
        for event in events
    } == {"re_fraud": "fraudulent", "re_duplicate": "duplicate"}


@pytest.mark.parametrize("raw", ["", "not json", "[]", json.dumps({"data": {"object": {"id": "re_other"}}})])
def test_legacy_inbox_refund_without_a_readable_reason_is_a_courtesy_refund(raw: str) -> None:
    event = _event(payload=raw, refund_reason="fraudulent")

    restored = adverse_event_from_payload(_legacy_payload(event))

    assert restored.refund_reason == ""
    assert adverse_event_latches(restored) is False
