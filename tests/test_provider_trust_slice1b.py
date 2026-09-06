from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.provider_trust_history import PROVIDER_SOURCES, provider_marker_qualifies
from trusted_router.services.adyen_trust import ADYEN_ADVERSE_CODES, adyen_adverse_events
from trusted_router.services.paypal_trust import PAYPAL_ADVERSE_EVENTS, paypal_adverse_events
from trusted_router.storage_models import CreditProvenance
from trusted_router.trust_reconciliation import BackfillMarker

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


def paypal_event(code: str = "PAYMENT.CAPTURE.REFUNDED", *, status: str | None = None, ref: str = "refund1", at: datetime = NOW) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "id": ref, "status": status or (code.rsplit(".", 1)[-1] if code.startswith("PAYMENT.REFUND.") else "COMPLETED"), "amount": {"currency_code": "USD", "value": "0.60"},
        "create_time": NOW.isoformat(), "update_time": at.isoformat(),
        "supplementary_data": {"related_ids": {"capture_id": "capture1"}},
    }
    if "DISPUTE" in code:
        resource.update(dispute_id=ref, status="OPEN", dispute_amount=resource["amount"],
                        disputed_transactions=[{"seller_transaction_id": "capture1"}])
        if code.endswith("RESOLVED"):
            resource.update(status="RESOLVED", dispute_outcome={"outcome_code": "RESOLVED_SELLER_FAVOUR"})
    if code.endswith("REVERSED"):
        resource["id"] = "capture1"
    return {"id": "WH-test", "event_type": code, "create_time": at.isoformat(), "resource": resource}


def adyen_item(code: str = "REFUND", *, ref: str = "mod1", at: datetime = NOW) -> dict[str, Any]:
    return {"pspReference": ref, "originalReference": "capture1", "eventCode": code,
            "success": "true", "amount": {"currency": "USD", "value": 60}, "eventDate": at.isoformat()}


def funded(provider: str, *, payment_first: bool = True) -> tuple[Any, Any, str]:
    store, database, _ = make_fake_store()
    workspace = store.create_workspace("owner", "provider-trust", trial_credit_microdollars=0)
    if payment_first:
        credit(store, workspace.id, provider)
    return store, database, workspace.id


def credit(store: Any, workspace: str, provider: str) -> None:
    assert store.credit_workspace_typed_direct(
        workspace, 1_000_000, "payment",
        provenance=CreditProvenance("capture" if provider == "paypal" else "authorisation", provider, "capture1", NOW),
        payment_amount_microdollars=1_200_000, currency="USD",
    )


def balance(database: Any, workspace: str) -> int:
    return sum(row["total_credits"] for (owner, _), row in database.typed["tr_credit_balance"].items() if owner == workspace)


@pytest.mark.parametrize("code", sorted(PAYPAL_ADVERSE_EVENTS))
@pytest.mark.parametrize("payment_first", [True, False])
def test_every_paypal_handler_recovers_once_and_drains_inbox(code: str, payment_first: bool) -> None:
    event, = paypal_adverse_events(paypal_event(code))
    store, database, workspace = funded("paypal", payment_first=payment_first)
    result = store.record_adverse_trust_event(event)
    if not payment_first:
        assert result.outcome == "inbox"
        assert store.record_adverse_trust_event(event).outcome == "inbox"
        credit(store, workspace, "paypal")
        assert not database.typed["tr_trust_inbox"]
    before = balance(database, workspace)
    assert store.record_adverse_trust_event(event).outcome == "replay"
    expected = 0 if event.lifecycle_status in {"pending", "failed", "reversed", "won"} else 500_000 if event.kind == "refund" else 1_000_000
    assert balance(database, workspace) == before == 1_000_000 - expected
    payment = database.typed["tr_trust_event"][(workspace, "payment")]
    assert payment["recovery_target"] == payment["recovered_micro"] + payment["unrecovered_micro"] == expected


@pytest.mark.parametrize("code", sorted(ADYEN_ADVERSE_CODES))
@pytest.mark.parametrize("payment_first", [True, False])
def test_every_adyen_handler_recovers_once_and_drains_inbox(code: str, payment_first: bool) -> None:
    event, = adyen_adverse_events(adyen_item(code))
    store, database, workspace = funded("adyen", payment_first=payment_first)
    result = store.record_adverse_trust_event(event)
    if not payment_first:
        assert result.outcome == "inbox"
        credit(store, workspace, "adyen")
        assert not database.typed["tr_trust_inbox"]
    expected = 0 if event.lifecycle_status in {"pending", "failed", "reversed", "won"} else 500_000 if event.kind == "refund" else 1_000_000
    assert balance(database, workspace) == 1_000_000 - expected
    assert store.record_adverse_trust_event(event).outcome == "replay"
    payment = database.typed["tr_trust_event"][(workspace, "payment")]
    assert payment["recovery_target"] == payment["recovered_micro"] + payment["unrecovered_micro"] == expected
    assert all(row["trust_latched_at"] is not None for (owner, _), row in database.typed["tr_credit_balance"].items() if owner == workspace)


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_ordering_partial_refunds_and_won_claim_preserve_recovery_invariant(provider: str) -> None:
    store, database, workspace = funded(provider)
    if provider == "paypal":
        refund, = paypal_adverse_events(paypal_event())
        dispute, = paypal_adverse_events(paypal_event("CUSTOMER.DISPUTE.CREATED", ref="dispute1"))
    else:
        refund, = adyen_adverse_events(adyen_item())
        dispute, = adyen_adverse_events(adyen_item("CHARGEBACK", ref="dispute1"))
    assert store.record_adverse_trust_event(dispute).recovery_target == 1_000_000
    assert store.record_adverse_trust_event(refund).recovery_target == 1_000_000
    won = dataclasses.replace(dispute, lifecycle_status="won", provider_ordering_watermark="z")
    assert store.record_adverse_trust_event(won).recovery_target == 500_000
    assert balance(database, workspace) == 500_000
    stale = dataclasses.replace(won, lifecycle_status="succeeded", provider_ordering_watermark="a")
    assert store.record_adverse_trust_event(stale).outcome == "stale"
    illegal = dataclasses.replace(stale, provider_ordering_watermark="zz")
    assert store.record_adverse_trust_event(illegal).outcome == "illegal"
    second = dataclasses.replace(refund, adverse_ref="refund:second", event_id="second")
    assert store.record_adverse_trust_event(second).recovery_target == 1_000_000
    assert balance(database, workspace) == 0


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_target_decrease_cancels_unrecovered_before_restoring(provider: str) -> None:
    store, database, workspace = funded(provider)
    for (owner, _), shard in database.typed["tr_credit_balance"].items():
        if owner == workspace:
            shard["total_usage"] = shard["total_credits"]
    event = (paypal_adverse_events(paypal_event("CUSTOMER.DISPUTE.CREATED"))[0] if provider == "paypal"
             else adyen_adverse_events(adyen_item("CHARGEBACK"))[0])
    result = store.record_adverse_trust_event(event)
    assert (result.recovered_micro, result.unrecovered_micro) == (0, 1_000_000)
    won = dataclasses.replace(event, lifecycle_status="won", provider_ordering_watermark="z")
    result = store.record_adverse_trust_event(won)
    assert (result.recovered_micro, result.unrecovered_micro, result.recovery_target) == (0, 0, 0)
    assert balance(database, workspace) == 1_000_000


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
@pytest.mark.parametrize("field,value", [("source", "wrong"), ("source_version", "old"), ("account_id", "other"), ("environment", "sandbox"), ("provider", "stripe"), ("completed_at", None), ("consistency_delay_seconds", 1)])
def test_marker_requires_exact_source_version_account_environment(provider: str, field: str, value: Any) -> None:
    source, version, delay = PROVIDER_SOURCES[provider]
    marker = BackfillMarker(provider, "merchant", "live", source, version, NOW - timedelta(days=100), NOW, delay, 0, 0, NOW)
    kwargs = dict(provider=provider, account_id="merchant", environment="live")
    assert provider_marker_qualifies(marker, **kwargs)
    assert not provider_marker_qualifies(None, **kwargs)
    assert not provider_marker_qualifies(dataclasses.replace(marker, **{field: value}), **kwargs)
    assert not provider_marker_qualifies(marker, payment_occurred_at=NOW - timedelta(days=101), **kwargs)


@pytest.mark.parametrize("provider,minimum", [("paypal", 12600), ("adyen", 1800)])
def test_max_age_enabled_provider_delay_plus_two_cadences(provider: str, minimum: int) -> None:
    with pytest.raises(ValidationError, match=str(minimum)):
        Settings(environment="test", trust_qualifying_providers=provider, trust_reconcile_max_age_seconds=minimum - 1)
    assert Settings(environment="test", trust_qualifying_providers=provider, trust_reconcile_max_age_seconds=minimum).trust_reconcile_max_age_seconds == minimum
    if provider == "paypal":
        with pytest.raises(ValidationError, match="default 3600 is invalid"):
            Settings(environment="test", trust_qualifying_providers="stripe,x402,paypal")


def test_qualification_and_arm_defaults_are_unchanged() -> None:
    settings = Settings(environment="test")
    assert settings.trust_qualifying_provider_set == {"stripe", "x402"}
    assert settings.spend_lease_trust_eligibility_enabled is False
    assert settings.trust_reconcile_max_age_seconds == 3600


@pytest.mark.parametrize("mutation", ["amount", "currency", "reference", "status", "timestamp"])
def test_paypal_rejects_malformed_adverse_without_acknowledging(mutation: str) -> None:
    event = paypal_event()
    resource = event["resource"]
    if mutation == "amount":
        resource["amount"]["value"] = "NaN"
    if mutation == "currency":
        resource["amount"]["currency_code"] = "EUR"
    if mutation == "reference":
        resource["supplementary_data"] = {}
    if mutation == "status":
        resource["status"] = "NEW_UNKNOWN_STATUS"
    if mutation == "timestamp":
        resource["update_time"] = "2026-09-05"
    with pytest.raises(HTTPException):
        paypal_adverse_events(event)


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
@pytest.mark.parametrize("reverse_delivery", [False, True])
def test_pending_then_completion_before_payment_is_not_swallowed(provider: str, reverse_delivery: bool) -> None:
    store, database, workspace = funded(provider, payment_first=False)
    succeeded = (paypal_adverse_events(paypal_event())[0] if provider == "paypal"
                 else adyen_adverse_events(adyen_item())[0])
    pending = dataclasses.replace(succeeded, lifecycle_status="pending", provider_ordering_watermark="a")
    succeeded = dataclasses.replace(succeeded, provider_ordering_watermark="b")
    events = (succeeded, pending) if reverse_delivery else (pending, succeeded)
    for event in events:
        assert store.record_adverse_trust_event(event).outcome == "inbox"
    assert len(database.typed["tr_trust_inbox"]) == 2
    credit(store, workspace, provider)
    assert not database.typed["tr_trust_inbox"]
    assert balance(database, workspace) == 500_000


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_postgres_inbox_drain_is_atomic_with_credit(provider: str) -> None:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn

    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = store.create_workspace("owner", "postgres-inbox", trial_credit_microdollars=0)
    event = (paypal_adverse_events(paypal_event())[0] if provider == "paypal"
             else adyen_adverse_events(adyen_item())[0])
    assert store.record_adverse_trust_event(event).outcome == "inbox"
    conn.fail_on = "DELETE FROM tr_trust_inbox"
    with pytest.raises(RuntimeError, match="connection reset"):
        credit(store, workspace.id, provider)
    conn.fail_on = None
    assert conn.execute("SELECT COUNT(*) FROM tr_trust_event WHERE kind='payment'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tr_trust_inbox").fetchone()[0] == 1
    credit(store, workspace.id, provider)
    assert conn.execute("SELECT COUNT(*) FROM tr_trust_inbox").fetchone()[0] == 0
    payment = conn.execute("SELECT recovery_target,recovered_micro,unrecovered_micro FROM tr_trust_event WHERE kind='payment'").fetchone()
    assert payment == (500_000, 500_000, 0)
    assert store.record_adverse_trust_event(event).outcome == "replay"


def test_adyen_failed_refund_restores_only_its_claim() -> None:
    store, database, workspace = funded("adyen")
    refund, = adyen_adverse_events(adyen_item())
    failed, = adyen_adverse_events(adyen_item("REFUND_FAILED", at=NOW + timedelta(seconds=1)))
    assert store.record_adverse_trust_event(refund).recovery_target == 500_000
    assert store.record_adverse_trust_event(failed).recovery_target == 0
    assert balance(database, workspace) == 1_000_000


def test_paypal_existing_route_verifies_before_adverse_mutation(monkeypatch: Any, client: Any) -> None:
    from trusted_router.routes.internal import paypal
    from trusted_router.storage import STORE

    workspace = STORE.create_workspace("owner", "paypal-route", trial_credit_microdollars=0)
    credit(STORE, workspace.id, "paypal")
    def reject(**_kwargs: Any) -> None:
        raise HTTPException(400, "invalid signature")
    monkeypatch.setattr(paypal, "verify_paypal_webhook_signature", reject)
    assert client.post("/v1/internal/paypal/webhook", json=paypal_event()).status_code == 400
    assert STORE.credit_money_snapshot(workspace.id) == (1_000_000, 0, 0)
    monkeypatch.setattr(paypal, "verify_paypal_webhook_signature", lambda **_kwargs: None)
    response = client.post("/v1/internal/paypal/webhook", json=paypal_event())
    assert response.status_code == 200
    assert response.json()["data"]["adverse"] == ["applied"]
    assert STORE.credit_money_snapshot(workspace.id) == (500_000, 0, 0)


def test_adyen_existing_route_validates_entire_batch_before_adverse_write() -> None:
    from fastapi.testclient import TestClient

    from tests.test_adyen_billing import (
        _notification_item,
        _settings,
        _webhook_payload,
        _workspace_id,
    )
    from trusted_router.main import create_app
    from trusted_router.services.adyen_billing import adyen_notification_signature
    from trusted_router.storage import STORE

    settings = _settings()
    with TestClient(create_app(settings, init_observability=False)) as client:
        workspace = _workspace_id(client)
        credit(STORE, workspace, "adyen")
        before = STORE.credit_money_snapshot(workspace)[0]
        good = _notification_item(workspace, event_code="REFUND", value=60)
        good.update(originalReference="capture1", eventDate=NOW.isoformat())
        good["additionalData"]["hmacSignature"] = adyen_notification_signature(good, settings.adyen_hmac_key)
        bad = {**good, "pspReference": "forged"}
        assert client.post("/v1/internal/adyen/webhook", json=_webhook_payload(good, bad)).status_code == 400
        assert STORE.credit_money_snapshot(workspace) == (before, 0, 0)
        assert client.post("/v1/internal/adyen/webhook", json=_webhook_payload(good)).status_code == 200
        assert STORE.credit_money_snapshot(workspace) == (before - 500_000, 0, 0)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("provider,code", [
    *[("paypal", code) for code in sorted(PAYPAL_ADVERSE_EVENTS)],
    *[("adyen", code) for code in sorted(ADYEN_ADVERSE_CODES)],
])
@pytest.mark.parametrize("payment_first", [False, True])
def test_provider_handlers_conform_on_other_backends(backend: str, provider: str, code: str, payment_first: bool) -> None:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    from trusted_router.storage import InMemoryStore

    conn = sqlite_postgres_conn() if backend == "postgres" else None
    store = InMemoryStore() if conn is None else postgres_store_on(conn)
    def snapshot(workspace_id: str) -> tuple[int, int, int]:
        if conn is None:
            return store.credit_money_snapshot(workspace_id)
        return conn.execute("SELECT SUM(total_credits), SUM(total_usage), SUM(reserved) FROM tr_credit_balance WHERE workspace_id=%s", (workspace_id,)).fetchone()
    workspace = store.create_workspace("owner", "conformance", trial_credit_microdollars=0)
    if payment_first:
        credit(store, workspace.id, provider)
    event, = paypal_adverse_events(paypal_event(code)) if provider == "paypal" else adyen_adverse_events(adyen_item(code))
    result = store.record_adverse_trust_event(event)
    if not payment_first:
        assert result.outcome == "inbox"
        credit(store, workspace.id, provider)
    expected = 0 if event.lifecycle_status in {"pending", "failed", "reversed", "won"} else 500_000 if event.kind == "refund" else 1_000_000
    result = store.record_adverse_trust_event(event)
    assert result.outcome == "replay"
    assert result.recovery_target == result.recovered_micro + result.unrecovered_micro == expected
    assert snapshot(workspace.id) == (1_000_000 - expected, 0, 0)
    stale = dataclasses.replace(event, lifecycle_status="pending" if event.lifecycle_status != "pending" else "succeeded", provider_ordering_watermark="0")
    assert store.record_adverse_trust_event(stale).outcome == "stale"
    assert snapshot(workspace.id) == (1_000_000 - expected, 0, 0)


@pytest.mark.parametrize("mutation", ["currency", "fractional_cents", "negative", "timestamp", "success"])
def test_adyen_malformed_adverse_is_rejected(mutation: str) -> None:
    item = adyen_item()
    if mutation == "currency":
        item["amount"]["currency"] = "EUR"
    elif mutation == "fractional_cents":
        item["amount"]["value"] = 0.5
    elif mutation == "negative":
        item["amount"]["value"] = -1
    elif mutation == "timestamp":
        item["eventDate"] = "2026-09-05"
    else:
        item["success"] = "unknown"
    with pytest.raises(HTTPException):
        adyen_adverse_events(item)


def test_adyen_missing_original_is_durable_unmatched_work() -> None:
    item = adyen_item()
    del item["originalReference"]
    del item["eventDate"]
    event, = adyen_adverse_events(item)
    store, database, workspace = funded("adyen")
    assert store.record_adverse_trust_event(event).outcome == "inbox"
    assert len(database.typed["tr_trust_inbox"]) == 1
    assert balance(database, workspace) == 1_000_000


def test_paypal_refund_up_link_resolves_and_conflicting_lineage_is_rejected() -> None:
    event = paypal_event()
    resource = event["resource"]
    resource["links"] = [{"rel": "up", "href": "https://api-m.paypal.com/v2/payments/captures/capture1"}]
    resource["supplementary_data"] = {}
    assert paypal_adverse_events(event)[0].original_payment_ref == "capture1"
    resource["supplementary_data"] = {"related_ids": {"capture_id": "different"}}
    with pytest.raises(HTTPException, match="disagree"):
        paypal_adverse_events(event)


def test_provider_inbox_identity_includes_kind_status_and_watermark() -> None:
    from trusted_router.services.provider_trust import provider_inbox_key

    event, = paypal_adverse_events(paypal_event())
    variants = [event, dataclasses.replace(event, kind="dispute"),
                dataclasses.replace(event, lifecycle_status="pending"),
                dataclasses.replace(event, provider_ordering_watermark="z")]
    assert len({provider_inbox_key(row) for row in variants}) == 4
    assert provider_inbox_key(event) == provider_inbox_key(dataclasses.replace(event, payload="ignored"))


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_same_raw_reference_across_kinds_and_providers_cannot_collide(provider: str) -> None:
    store, database, workspace = funded(provider)
    refund = paypal_adverse_events(paypal_event(ref="shared"))[0] if provider == "paypal" else adyen_adverse_events(adyen_item(ref="shared"))[0]
    dispute = paypal_adverse_events(paypal_event("CUSTOMER.DISPUTE.CREATED", ref="shared"))[0] if provider == "paypal" else adyen_adverse_events(adyen_item("CHARGEBACK", ref="shared"))[0]
    assert store.record_adverse_trust_event(refund).recovery_target == 500_000
    assert store.record_adverse_trust_event(dispute).recovery_target == 1_000_000
    assert len(database.typed["tr_trust_event"]) == 3
    assert balance(database, workspace) == 0


# Literal decision-76 contract, independent of the implementation's dispatch maps.
PROVIDER_EVENT_CONTRACT = [
    ("paypal", "PAYMENT.CAPTURE.REFUNDED", "refund", "refund", "succeeded"),
    ("paypal", "PAYMENT.CAPTURE.REVERSED", "dispute", "reversal", "lost"),
    ("paypal", "PAYMENT.REFUND.PENDING", "refund", "refund", "pending"),
    ("paypal", "PAYMENT.REFUND.COMPLETED", "refund", "refund", "succeeded"),
    ("paypal", "PAYMENT.REFUND.FAILED", "refund", "refund", "reversed"),
    ("paypal", "CUSTOMER.DISPUTE.CREATED", "dispute", "dispute", "succeeded"),
    ("paypal", "CUSTOMER.DISPUTE.UPDATED", "dispute", "dispute", "succeeded"),
    ("paypal", "CUSTOMER.DISPUTE.RESOLVED", "dispute", "dispute", "won"),
    ("adyen", "REFUND", "refund", "refund", "succeeded"),
    ("adyen", "REFUND_FAILED", "refund", "refund", "reversed"),
    ("adyen", "REFUNDED_REVERSED", "dispute", "refund_reversal", "lost"),
    ("adyen", "CANCEL_OR_REFUND", "dispute", "cancellation", "lost"),
    ("adyen", "CANCELLATION", "dispute", "cancellation", "lost"),
    ("adyen", "TECHNICAL_CANCEL", "dispute", "cancellation", "lost"),
    ("adyen", "CAPTURE_FAILED", "dispute", "capture_failure", "lost"),
    ("adyen", "CAPTURE_REVERSED", "dispute", "capture_reversal", "lost"),
    ("adyen", "NOTIFICATION_OF_FRAUD", "dispute", "fraud", "pending"),
    ("adyen", "NOTIFICATION_OF_CHARGEBACK", "dispute", "chargeback", "pending"),
    ("adyen", "CHARGEBACK", "dispute", "chargeback", "succeeded"),
    ("adyen", "CHARGEBACK_REVERSED", "dispute", "chargeback", "won"),
    ("adyen", "SECOND_CHARGEBACK", "dispute", "second_chargeback", "lost"),
]


@pytest.mark.parametrize("provider,code,kind,subtype,status", PROVIDER_EVENT_CONTRACT)
def test_provider_event_contract(provider: str, code: str, kind: str, subtype: str, status: str) -> None:
    event, = paypal_adverse_events(paypal_event(code)) if provider == "paypal" else adyen_adverse_events(adyen_item(code))
    assert (event.provider, event.kind, event.provider_subtype, event.lifecycle_status) == (provider, kind, subtype, status)
    assert event.original_payment_ref == "capture1"
    assert event.amount_micro == 600_000
    assert {row[1] for row in PROVIDER_EVENT_CONTRACT if row[0] == "paypal"} == PAYPAL_ADVERSE_EVENTS
    assert {row[1] for row in PROVIDER_EVENT_CONTRACT if row[0] == "adyen"} == set(ADYEN_ADVERSE_CODES)


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
@pytest.mark.parametrize("payment_first", [False, True])
@pytest.mark.parametrize("reverse_delivery", [False, True])
def test_equal_timestamp_refund_reversal_converges_in_either_delivery_order(provider: str, payment_first: bool, reverse_delivery: bool) -> None:
    if provider == "paypal":
        events = [paypal_adverse_events(paypal_event(status=status))[0]
                  for status in ("PENDING", "COMPLETED", "FAILED")]
    else:
        from trusted_router.services.provider_trust import ordering_watermark

        succeeded, = adyen_adverse_events(adyen_item())
        pending = dataclasses.replace(succeeded, lifecycle_status="pending",
                                      provider_ordering_watermark=ordering_watermark(NOW, "pending"))
        reversed_event, = adyen_adverse_events(adyen_item("REFUND_FAILED"))
        events = [pending, succeeded, reversed_event]
    assert events[0].provider_ordering_watermark < events[1].provider_ordering_watermark < events[2].provider_ordering_watermark
    store, database, workspace = funded(provider, payment_first=payment_first)
    for event in reversed(events) if reverse_delivery else events:
        store.record_adverse_trust_event(event)
    if not payment_first:
        credit(store, workspace, provider)
        assert not database.typed["tr_trust_inbox"]
    result = store.record_adverse_trust_event(events[-1])
    assert result.outcome == "replay"
    assert result.recovery_target == result.recovered_micro == result.unrecovered_micro == 0
    assert balance(database, workspace) == 1_000_000
