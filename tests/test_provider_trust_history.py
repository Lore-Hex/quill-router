from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.test_provider_trust_slice1b import NOW, adyen_item, paypal_event
from tests.test_trust_reconciliation_slice1d import _Repository
from trusted_router.adyen_trust_history import AdyenAccountingSource
from trusted_router.paypal_trust_history import (
    history_start,
    paypal_payment,
    refetch_paypal_adverse,
    scan_paypal_created_range,
    transaction_windows,
)
from trusted_router.provider_trust_history import (
    PROVIDER_SOURCES,
    provider_marker_qualifies,
    provider_scan,
)
from trusted_router.provider_trust_reconcile import run_provider_backfill, run_provider_recurring
from trusted_router.services.adyen_billing import _new_checkout_reference
from trusted_router.services.adyen_trust import adyen_adverse_events
from trusted_router.services.paypal_trust import paypal_adverse_events
from trusted_router.storage_models import CreditProvenance
from trusted_router.trust_reconciliation import BackfillMarker, OutstandingAdverse
from trusted_router.trust_tiers import payment_or_grant_event

START = NOW - timedelta(days=60)
KEY = "report-reference-key-with-at-least-32-bytes"


def capture() -> dict[str, Any]:
    return {"id": "capture1", "status": "COMPLETED", "create_time": START.isoformat(),
            "amount": {"currency_code": "USD", "value": "1.20"}, "custom_id": "tr1|workspace|100|120"}


class PayPalAPI:
    def __init__(self, *, missing: bool = False, pages: int = 1) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.missing = missing
        self.pages = pages

    def get(self, path: str, params: Any = None) -> dict[str, Any]:
        self.calls.append((path, params))
        if path == "/v1/reporting/transactions":
            return {"total_pages": self.pages, "transaction_details": [{"transaction_info": {
                "paypal_account_id": "merchant", "transaction_id": "refund1", "transaction_event_code": "T1107",
                "transaction_initiation_date": (NOW - timedelta(days=1)).isoformat(),
                "transaction_updated_date": (NOW - timedelta(hours=4)).isoformat(),
            }}]}
        if path == "/v1/customer/disputes":
            return {"items": [], "links": []}
        if path == "/v2/payments/captures/capture1":
            if self.missing:
                raise OSError("capture unavailable")
            return capture()
        if path == "/v2/payments/refunds/refund1":
            resource = paypal_event(at=NOW - timedelta(hours=4))["resource"]
            resource["create_time"] = (NOW - timedelta(days=1)).isoformat()
            return resource
        raise AssertionError(path)


def report(*, record: str = "Refunded", modification: str = "mod1") -> list[dict[str, str]]:
    merchant = _new_checkout_reference(workspace_id="00000000-0000-0000-0000-000000000001", credit_amount_cents=100, charge_amount_cents=120, reference_key=KEY)
    payment = {"Merchant Account": "merchant", "Psp Reference": "capture1", "Merchant Reference": merchant,
               "Record Type": "Authorised", "Booking Date": START.isoformat(), "TimeZone": "UTC",
               "Main Currency": "USD", "Main Amount": "1.20", "Modification Psp Reference": ""}
    adverse = {**payment, "Record Type": record, "Booking Date": (NOW - timedelta(hours=4)).isoformat(),
               "Main Amount": "-0.60", "Modification Psp Reference": modification}
    return [payment, adverse]


def accounting(rows: Any) -> AdyenAccountingSource:
    return AdyenAccountingSource(rows, account_id="merchant", environment="live", covered_from=START,
                                 covered_through=NOW, reference_key=KEY)


def test_paypal_windows_delay_calendar_year_limit_and_leap_day() -> None:
    end = NOW - timedelta(hours=3)
    windows = list(transaction_windows(START, end, now=NOW))
    assert len(windows) == 2
    assert windows[0][1] == windows[1][0]
    assert all(stop - begin <= timedelta(days=31) for begin, stop in windows)
    assert windows[-1][1] == end
    with pytest.raises(ValueError, match="three-hour"):
        list(transaction_windows(START, NOW, now=NOW))
    with pytest.raises(ValueError, match="three years"):
        list(transaction_windows(NOW.replace(year=2022), end, now=NOW))
    leap = datetime(2024, 2, 29, tzinfo=UTC)
    assert history_start(leap.replace(year=2020), now=leap) == datetime(2021, 2, 28, tzinfo=UTC)


def test_paypal_search_paginates_and_source_principal_excludes_fee() -> None:
    client = PayPalAPI(pages=2)
    scan = scan_paypal_created_range(client, account_id="merchant", start=START, end=NOW - timedelta(hours=3), recorded_at=NOW)
    assert not scan.unmatched_ids
    assert len(scan.adverse) == len(scan.payments) == 1
    assert scan.payments[0].credited_micro == 1_000_000
    assert scan.payments[0].payment_amount_micro == 1_200_000
    assert len([call for call in client.calls if call[0] == "/v1/reporting/transactions"]) == 4


def test_paypal_missing_original_never_produces_completed_marker() -> None:
    scan = scan_paypal_created_range(PayPalAPI(missing=True), account_id="merchant", start=START, end=NOW - timedelta(hours=3), recorded_at=NOW)
    assert scan.unmatched_ids == ("refund1",)


@pytest.mark.parametrize("record,kind,subtype", [("Refunded", "refund", "refund"), ("Cancelled", "dispute", "cancellation"), ("NotificationOfFraud", "dispute", "fraud"), ("Chargeback", "dispute", "chargeback"), ("RefundedReversed", "dispute", "refund_reversal")])
def test_adyen_report_preserves_authorisation_and_modification_references(record: str, kind: str, subtype: str) -> None:
    scan = accounting(report(record=record)).scan(START, NOW, NOW)
    assert not scan.unmatched_ids
    event, = scan.adverse
    assert (event.original_payment_ref, event.adverse_ref, event.kind) == ("capture1", f"{subtype}:mod1", kind)
    assert scan.payments[0].credited_micro == 1_000_000


def test_adyen_report_missing_lineage_unknown_record_and_coverage_fail_closed() -> None:
    rows = report(modification="")
    assert accounting(rows).scan(START, NOW, NOW).unmatched_ids
    assert accounting(report(record="UnknownAdverse")).scan(START, NOW, NOW).unmatched_ids
    with pytest.raises(ValueError, match="cover"):
        accounting(report()).scan(START - timedelta(days=1), NOW, NOW)
    rows[0]["Merchant Account"] = "wrong"
    with pytest.raises(ValueError, match="merchant"):
        accounting(rows)


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_backfill_marker_semantic_hash_and_drain_window(provider: str) -> None:
    repository = _Repository()
    scan = (scan_paypal_created_range(PayPalAPI(), account_id="merchant", start=START, end=NOW - timedelta(hours=3), recorded_at=NOW)
            if provider == "paypal" else accounting(report()).scan(START, NOW, NOW))
    kwargs = dict(provider=provider, account_id="merchant", environment="live", history_start=START,
                  drained_at=NOW - timedelta(hours=4), now=NOW)
    repository.stripe_events.update(p.event_id for p in scan.payments)
    result = run_provider_backfill(repository, lambda start, end: scan, **kwargs)
    assert result.marker.completed_at == NOW
    assert provider_marker_qualifies(result.marker, provider=provider, account_id="merchant", environment="live")
    # Same IDs, wrong amount: an ID-set-only completion proof would miss this.
    stored = next(event for event in repository.events.values() if event.kind == "payment")
    stored.credited_micro = 2_000_000
    result = run_provider_backfill(repository, lambda start, end: scan, **kwargs)
    assert result.marker.semantic_mismatch_count > 0
    assert result.marker.completed_at is None
    with pytest.raises(ValueError, match="drained revision"):
        run_provider_backfill(repository, lambda start, end: scan, **{**kwargs, "drained_at": NOW + timedelta(seconds=1)})


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
@pytest.mark.parametrize("fail_refetch", [False, True])
def test_recurring_refetches_old_pending_ids_and_holds_watermark_on_failure(provider: str, fail_refetch: bool) -> None:
    repository = _Repository()
    source, version, delay = PROVIDER_SOURCES[provider]
    payment = (paypal_payment(capture(), recorded_at=NOW) if provider == "paypal" else payment_or_grant_event(
        "workspace", "payment", 1_000_000, CreditProvenance("authorisation", "adyen", "capture1", START),
        recorded_at=NOW, payment_amount_microdollars=1_200_000, currency="USD"))
    event = (paypal_adverse_events(paypal_event())[0] if provider == "paypal" else adyen_adverse_events(adyen_item())[0])
    event = dataclasses.replace(event, lifecycle_status="pending", occurred_at=START + timedelta(days=1), provider_ordering_watermark="a")
    repository.stripe_events.add(payment.event_id)
    repository.write_payment_fact(payment, evidence_ids=(payment.event_id,))
    repository.write_adverse_fact(event)
    old = NOW - timedelta(days=1)
    repository.save_marker(BackfillMarker(provider, "merchant", "live", source, version, START, old, delay, 0, 0, old))
    calls: list[str] = []
    ranges: list[Any] = []
    def scan(start: datetime, end: datetime) -> Any:
        ranges.append((start, end))
        return provider_scan([], [], recorded_at=NOW)
    def refetch(row: OutstandingAdverse, at: datetime) -> Any:
        calls.append(row.adverse_ref)
        if fail_refetch:
            raise OSError("provider unavailable")
        updated = dataclasses.replace(event, lifecycle_status="succeeded", provider_ordering_watermark="z")
        return updated, dataclasses.replace(row, lifecycle_status="succeeded")
    result = run_provider_recurring(repository, scan, refetch, provider=provider, account_id="merchant",
                                    environment="live", cadence_seconds=900, now=NOW, alert_horizon=lambda row: None)
    assert calls == [event.adverse_ref]
    assert ranges[0][0] == old - timedelta(seconds=delay + 1800)
    assert result.watermark_advanced is (not fail_refetch)
    assert result.marker.closed_through == (old if fail_refetch else NOW - timedelta(seconds=delay))


def test_refetch_paypal_uses_object_id_and_validates_original() -> None:
    row = OutstandingAdverse("paypal", "refund", "refund:refund1", "capture1", "pending", START)
    event, _ = refetch_paypal_adverse(PayPalAPI(), row, NOW)
    assert event.adverse_ref == "refund:refund1"
    with pytest.raises(ValueError, match="identity"):
        refetch_paypal_adverse(PayPalAPI(), dataclasses.replace(row, original_payment_ref="wrong"), NOW)


def test_paypal_account_mismatch_never_writes_a_marker() -> None:
    with pytest.raises(ValueError, match="merchant account"):
        scan_paypal_created_range(PayPalAPI(), account_id="wrong", start=START,
                                  end=NOW - timedelta(hours=3), recorded_at=NOW)


def test_paypal_dispute_without_balance_transaction_is_enumerated() -> None:
    class DisputeAPI(PayPalAPI):
        def get(self, path: str, params: Any = None) -> dict[str, Any]:
            if path == "/v1/customer/disputes":
                return {"items": [{"dispute_id": "dispute1"}]}
            if path == "/v1/customer/disputes/dispute1":
                resource = paypal_event("CUSTOMER.DISPUTE.CREATED", ref="dispute1")["resource"]
                resource["create_time"] = (NOW - timedelta(days=1)).isoformat()
                return resource
            return super().get(path, params)
    scan = scan_paypal_created_range(DisputeAPI(), account_id="merchant", start=START,
                                    end=NOW - timedelta(hours=3), recorded_at=NOW)
    assert not scan.unmatched_ids
    assert {event.kind for event in scan.adverse} == {"refund", "dispute"}


def test_adyen_outstanding_report_retrieval_revisits_old_created_id() -> None:
    rows = report(record="SentForRefund")
    old = rows[1]
    old["Booking Date"] = (START + timedelta(days=1)).isoformat()
    current = {**old, "Record Type": "Refunded", "Booking Date": (NOW - timedelta(hours=1)).isoformat()}
    source = accounting([*rows, current])
    row = OutstandingAdverse("adyen", "refund", "refund:mod1", "capture1", "pending", START + timedelta(days=1))
    event, refreshed = source.refetch(row, NOW)
    assert event.lifecycle_status == refreshed.lifecycle_status == "succeeded"
    assert event.original_payment_ref == "capture1"


def test_provider_refetch_does_not_invent_a_mutation_horizon() -> None:
    from trusted_router.provider_trust_reconcile import ProviderOutstandingAdverse

    row = ProviderOutstandingAdverse("adyen", "dispute", "fraud:mod1", "capture1", "pending", START)
    assert row.horizon_at == datetime.max.replace(tzinfo=UTC)
    proven = dataclasses.replace(row, evidence_deadline=NOW)
    assert proven.horizon_at == NOW


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_postgres_history_records_fact_without_minting_and_drains_inbox(provider: str) -> None:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    from trusted_router.storage_trust_reconciliation import trust_reconciliation_repository

    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = store.create_workspace("owner", "history", trial_credit_microdollars=0)
    event = (paypal_adverse_events(paypal_event())[0] if provider == "paypal" else adyen_adverse_events(adyen_item())[0])
    assert store.record_adverse_trust_event(event).outcome == "inbox"
    payment = payment_or_grant_event(
        workspace.id, "historical", 1_000_000,
        CreditProvenance("capture" if provider == "paypal" else "authorisation", provider, "capture1", START),
        recorded_at=NOW, payment_amount_microdollars=1_200_000, currency="USD")
    repository = trust_reconciliation_repository(store)
    assert repository.write_payment_fact(payment) == "uncredited"
    store._run_transaction(lambda c: store._write_entity_tx(c, "stripe_event", "old_credit", {"created_at": START.isoformat()}))
    assert repository.write_payment_fact(payment, evidence_ids=("old_credit",)) == "inserted"
    assert repository.write_payment_fact(payment, evidence_ids=("old_credit",)) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM tr_trust_inbox").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(total_credits) FROM tr_credit_balance").fetchone()[0] == 0
    assert conn.execute("SELECT recovery_target,unrecovered_micro FROM tr_trust_event WHERE kind='payment'").fetchone() == (500_000, 500_000)


@pytest.mark.parametrize("corruption", ["capture_id", "refund_id", "page_count"])
def test_paypal_inconsistent_canonical_responses_fail_completion(corruption: str) -> None:
    class BrokenAPI(PayPalAPI):
        def get(self, path: str, params: Any = None) -> dict[str, Any]:
            body = super().get(path, params)
            if corruption == "capture_id" and path.startswith("/v2/payments/captures/"):
                body["id"] = "another_capture"
            elif corruption == "refund_id" and path.startswith("/v2/payments/refunds/"):
                body["id"] = "another_refund"
            elif corruption == "page_count" and path == "/v1/reporting/transactions":
                body["total_pages"] = 2 if params["page"] == 1 else 1
            return body
    if corruption == "page_count":
        with pytest.raises(ValueError, match="page count changed"):
            scan_paypal_created_range(BrokenAPI(), account_id="merchant", start=START,
                                      end=NOW - timedelta(hours=3), recorded_at=NOW)
    else:
        scan = scan_paypal_created_range(BrokenAPI(), account_id="merchant", start=START,
                                        end=NOW - timedelta(hours=3), recorded_at=NOW)
        assert scan.unmatched_ids


def test_paypal_empty_transaction_search_is_a_valid_closed_scan() -> None:
    class EmptyAPI(PayPalAPI):
        def get(self, path: str, params: Any = None) -> dict[str, Any]:
            if path == "/v1/reporting/transactions":
                return {"total_pages": 0, "transaction_details": []}
            return super().get(path, params)
    scan = scan_paypal_created_range(EmptyAPI(), account_id="merchant", start=START,
                                    end=NOW - timedelta(hours=3), recorded_at=NOW)
    assert scan.payments == scan.adverse == scan.unmatched_ids == ()


@pytest.mark.parametrize("conflict", ["amount", "lineage", "illegal_transition"])
def test_provider_scan_rejects_conflicting_or_illegal_observations(conflict: str) -> None:
    payment = paypal_payment(capture(), recorded_at=NOW)
    event, = paypal_adverse_events(paypal_event())
    other = dataclasses.replace(event, amount_micro=1 if conflict == "amount" else event.amount_micro,
                                original_payment_ref="other" if conflict == "lineage" else event.original_payment_ref)
    if conflict == "illegal_transition":
        other = dataclasses.replace(event, lifecycle_status="pending", provider_ordering_watermark="z")
    scan = provider_scan([payment], [event, other], recorded_at=NOW)
    assert scan.unmatched_ids


@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_recurring_requires_complete_expected_marker_and_validates_refetch_identity(provider: str) -> None:
    repository = _Repository()
    source, version, delay = PROVIDER_SOURCES[provider]
    kwargs = dict(provider=provider, account_id="merchant", environment="live", cadence_seconds=900,
                  now=NOW, alert_horizon=lambda row: None)
    def empty(start: datetime, end: datetime) -> Any:
        return provider_scan([], [], recorded_at=NOW)
    with pytest.raises(ValueError, match="marker"):
        run_provider_recurring(repository, empty, lambda row, now: None, **kwargs)
    payment = paypal_payment(capture(), recorded_at=NOW) if provider == "paypal" else accounting(report()).scan(START, NOW, NOW).payments[0]
    event = paypal_adverse_events(paypal_event())[0] if provider == "paypal" else adyen_adverse_events(adyen_item())[0]
    event = dataclasses.replace(event, lifecycle_status="pending", occurred_at=START)
    repository.stripe_events.add(payment.event_id)
    repository.write_payment_fact(payment, evidence_ids=(payment.event_id,))
    repository.write_adverse_fact(event)
    old = NOW - timedelta(days=1)
    marker = BackfillMarker(provider, "merchant", "live", source, version, START, old, delay, 0, 0, old)
    repository.save_marker(marker)
    def wrong(row: OutstandingAdverse, now: datetime) -> Any:
        return dataclasses.replace(event, original_payment_ref="other"), row
    result = run_provider_recurring(repository, empty, wrong, **kwargs)
    assert not result.watermark_advanced
    assert repository.get_marker(provider, "merchant", "live", source, version) == marker


def test_adyen_report_csv_reader_validates_columns(tmp_path: Any) -> None:
    import csv

    from trusted_router.adyen_trust_history import read_payment_accounting_report

    path = tmp_path / "accounting.csv"
    rows = report()
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert read_payment_accounting_report(path) == tuple(rows)
    path.write_text("Merchant Account,Psp Reference\nmerchant,payment\n")
    with pytest.raises(ValueError, match="columns"):
        read_payment_accounting_report(path)


def test_packaged_provider_reconciler_is_shipped_and_help_is_read_only(capsys: Any) -> None:
    from pathlib import Path

    from trusted_router.provider_trust_cli import main

    root = Path(__file__).parents[1]
    assert "COPY src ./src" in (root / "Dockerfile").read_text()
    assert "from trusted_router.provider_trust_cli import main" in (root / "scripts/reconcile_provider_trust.py").read_text()
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "--drained-at" in capsys.readouterr().out


def test_paypal_capture_listing_cannot_substitute_another_payment_id() -> None:
    class WrongCaptureAPI(PayPalAPI):
        def get(self, path: str, params: Any = None) -> dict[str, Any]:
            if path == "/v1/reporting/transactions":
                return {"total_pages": 1, "transaction_details": [{"transaction_info": {
                    "paypal_account_id": "merchant", "transaction_id": "capture1", "transaction_event_code": "T0006",
                    "transaction_initiation_date": START.isoformat(),
                }}]}
            if path == "/v2/payments/captures/capture1":
                return {**capture(), "id": "substituted"}
            return super().get(path, params)
    scan = scan_paypal_created_range(WrongCaptureAPI(), account_id="merchant", start=START,
                                    end=NOW - timedelta(hours=3), recorded_at=NOW)
    assert scan.unmatched_ids == ("capture1",)
    assert not scan.payments


def test_provider_refetch_rejects_wrong_identity_before_transaction(monkeypatch: Any) -> None:
    from trusted_router import provider_trust_reconcile

    repository = _Repository()
    source, version, delay = PROVIDER_SOURCES["paypal"]
    repository.save_marker(BackfillMarker("paypal", "merchant", "live", source, version,
                                         START, NOW - timedelta(days=1), delay, 0, 0, NOW))
    event, = paypal_adverse_events(paypal_event())
    row = OutstandingAdverse("paypal", "refund", event.adverse_ref, "capture1", "pending", START)
    def intercept(repository: Any, scan: Any, refetch: Any, **kwargs: Any) -> Any:
        with pytest.raises(ValueError, match="identity changed"):
            refetch(row, NOW)
    monkeypatch.setattr(provider_trust_reconcile, "run_recurring_reconciliation", intercept)
    run_provider_recurring(
        repository, lambda start, end: provider_scan([], [], recorded_at=NOW),
        lambda row, now: (dataclasses.replace(event, original_payment_ref="wrong"), row),
        provider="paypal", account_id="merchant", environment="live", cadence_seconds=900,
        now=NOW, alert_horizon=lambda row: None,
    )


def test_unclassified_paypal_activity_cannot_silently_complete_history() -> None:
    class UnknownAPI(PayPalAPI):
        def get(self, path: str, params: Any = None) -> dict[str, Any]:
            body = super().get(path, params)
            if path == "/v1/reporting/transactions":
                body["transaction_details"][0]["transaction_info"]["transaction_event_code"] = "T9999"
            return body
    scan = scan_paypal_created_range(UnknownAPI(), account_id="merchant", start=START,
                                    end=NOW - timedelta(hours=3), recorded_at=NOW)
    assert scan.unmatched_ids == ("refund1",)


def test_adyen_settlement_reversal_claims_principal_and_expiration_needs_lineage() -> None:
    scan = accounting(report(record="SettledReversed")).scan(START, NOW, NOW)
    assert not scan.unmatched_ids
    event, = scan.adverse
    assert (event.kind, event.provider_subtype, event.lifecycle_status) == ("dispute", "capture_reversal", "lost")
    assert accounting(report(record="Expired")).scan(START, NOW, NOW).unmatched_ids


@pytest.mark.parametrize("backend", ["spanner", "postgres"])
@pytest.mark.parametrize("provider", ["paypal", "adyen"])
def test_actual_transactional_writers_complete_source_hash_proof(backend: str, provider: str) -> None:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    from tests.fakes.spanner import make_fake_store
    from trusted_router.storage_trust_reconciliation import trust_reconciliation_repository

    store = make_fake_store()[0] if backend == "spanner" else postgres_store_on(sqlite_postgres_conn())
    workspace = store.create_workspace("owner", "source-proof", trial_credit_microdollars=0)
    if provider == "paypal":
        class API(PayPalAPI):
            def get(self, path: str, params: Any = None) -> dict[str, Any]:
                body = super().get(path, params)
                if path == "/v2/payments/captures/capture1":
                    body["custom_id"] = f"tr1|{workspace.id}|100|120"
                return body
        scan = scan_paypal_created_range(API(), account_id="merchant", start=START,
                                        end=NOW - timedelta(hours=3), recorded_at=NOW)
    else:
        rows = report()
        reference = _new_checkout_reference(workspace_id=workspace.id, credit_amount_cents=100,
                                           charge_amount_cents=120, reference_key=KEY)
        for row in rows:
            row["Merchant Reference"] = reference
        scan = accounting(rows).scan(START, NOW, NOW)
    payment, = scan.payments
    assert store.credit_workspace_typed_direct(
        workspace.id, payment.credited_micro, payment.event_id,
        provenance=CreditProvenance(payment.provider_subtype, provider, "capture1", payment.occurred_at),
        payment_amount_microdollars=payment.payment_amount_micro, currency="USD")
    # A live webhook has already applied the fact: the source and actual stored
    # canonical hashes must still match, including stamps and recovery target.
    event, = scan.adverse
    assert store.record_adverse_trust_event(event).recovery_target == 500_000
    repository = trust_reconciliation_repository(store)
    for _ in range(2):
        result = run_provider_backfill(repository, lambda start, end: scan, provider=provider,
                                       account_id="merchant", environment="live", history_start=START,
                                       drained_at=NOW - timedelta(hours=4), now=NOW)
        assert (result.marker.unmatched_count, result.marker.semantic_mismatch_count) == (0, 0)
        assert result.marker.completed_at == NOW
        assert store.record_adverse_trust_event(event).recovery_target == 500_000
