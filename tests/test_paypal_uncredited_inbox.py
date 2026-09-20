from __future__ import annotations

import dataclasses
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.services import paypal_inbox, trust_recovery
from trusted_router.services.paypal_inbox import (
    RefundedCaptureProof,
    reconcile_uncredited_paypal_inbox,
    verify_refunded_capture,
)
from trusted_router.services.provider_trust import observation
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import CreditProvenance
from trusted_router.storage_trust_inbox_resolution import record_refunded_uncredited
from trusted_router.trust_tier_cli import run as run_tier_job

NOW = datetime(2030, 1, 1, tzinfo=UTC)
CREATED = NOW - timedelta(days=2)
CHARGED = 206_290_000
CREDIT = 200_000_000
SETTINGS = SimpleNamespace(**{"paypal_client_id": "fake-id", "paypal_client_secret": "fake-secret"})


def adverse(kind: str = "refund", status: str = "succeeded", *, minute: int = 1,
            amount: int = CHARGED, capture: str = "CAPTURE1", ref: str | None = None) -> Any:
    return observation(provider="paypal", reference=ref or ("REFUND1" if kind == "refund" else "DISPUTE1"),
                       payment=capture, kind=kind, subtype=kind, status=status, amount=amount,
                       created=CREATED, updated=CREATED + timedelta(minutes=minute))


@pytest.fixture(params=["memory", "spanner", "postgres"])
def backend(request: Any) -> Any:
    if request.param == "memory":
        store = InMemoryStore()
        def balance(ws: str) -> int:
            return int(store.credit_money_snapshot(ws)[0])
    elif request.param == "spanner":
        store, database, _ = make_fake_store()
        def balance(ws: str) -> int:
            return sum(int(row["total_credits"]) for (owner, _), row in
                       database.typed["tr_credit_balance"].items() if owner == ws)
    else:
        conn = sqlite_postgres_conn()
        store = postgres_store_on(conn)
        def balance(ws: str) -> int:
            return int(conn.execute("SELECT SUM(total_credits) FROM tr_credit_balance "
                                    "WHERE workspace_id=%s", (ws,)).fetchone()[0])
    workspace = store.create_workspace("owner", "refund regression", trial_credit_microdollars=0)
    yield store, workspace.id, balance
    if request.param == "postgres":
        conn._raw.close()


class API:
    def __init__(self, workspace_id: str) -> None:
        self.calls: list[str] = []
        self.capture: dict[str, Any] = {
            "id": "CAPTURE1", "status": "REFUNDED", "create_time": CREATED.isoformat(),
            "custom_id": json.dumps({"w": workspace_id, "u": "owner", "c": 20000, "t": 20629}),
            "amount": {"currency_code": "USD", "value": "206.29"},
        }
        self.refund: dict[str, Any] = {
            "id": "REFUND1", "status": "COMPLETED", "create_time": CREATED.isoformat(),
            "update_time": (CREATED + timedelta(minutes=3)).isoformat(),
            "amount": {"currency_code": "USD", "value": "206.29"},
            "supplementary_data": {"related_ids": {"capture_id": "CAPTURE1"}},
        }

    def get(self, path: str, params: Any = None) -> dict[str, Any]:
        self.calls.append(path)
        if path == "/v2/payments/captures/CAPTURE1":
            return self.capture
        assert path == "/v2/payments/refunds/REFUND1"
        return self.refund


def inbox(store: Any) -> tuple[Any, ...]:
    return store.list_stale_trust_inbox(older_than=NOW)


def enqueue(store: Any) -> tuple[Any, ...]:
    for event in (adverse("dispute", minute=0), adverse("dispute", minute=1), adverse(minute=2),
                  adverse("dispute", minute=3), adverse("dispute", "lost", minute=4)):
        assert store.record_adverse_trust_event(event).outcome == "inbox"
    return inbox(store)


def credit(store: Any, workspace: str) -> bool:
    return store.credit_workspace_typed_direct(
        workspace, CREDIT, "paypal_capture:CAPTURE1",
        provenance=CreditProvenance("capture", "paypal", "CAPTURE1", CREATED),
        payment_amount_microdollars=CHARGED, currency="USD",
    )


def test_refunded_uncredited_capture_reconciles_without_moving_money(backend: Any, monkeypatch: Any) -> None:
    store, ws, balance = backend
    rows = enqueue(store)
    assert len(rows) == 5
    alerts: list[str] = []
    monkeypatch.setattr(trust_recovery, "ops_alert", lambda message, **kw: alerts.append(message))
    assert trust_recovery.alert_stale_trust_inbox(store, now=NOW) == 5
    api = API(ws)
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=api) == 5
    assert balance(ws) == 0
    assert trust_recovery.alert_stale_trust_inbox(store, now=NOW) == 0
    assert len(alerts) == 5
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=api) == 0
    assert len(api.calls) == 2


def test_delayed_completed_callback_still_recovers_full_principal_exactly_once(backend: Any) -> None:
    store, ws, balance = backend
    enqueue(store)
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=API(ws)) == 5
    # Classification must NOT remove the inbox: it is still consumed by the
    # atomic credit path if a delayed valid completion callback arrives.
    assert credit(store, ws)
    assert balance(ws) == 0
    assert not credit(store, ws)
    assert balance(ws) == 0
    assert not inbox(store)


def test_completion_racing_with_verification_is_not_misclassified(backend: Any) -> None:
    store, ws, balance = backend
    rows = enqueue(store)
    proof = verify_refunded_capture(API(ws), rows, now=NOW)
    assert proof is not None
    assert credit(store, ws)
    assert record_refunded_uncredited(store, proof, rows) == 0
    assert balance(ws) == 0


@pytest.mark.parametrize("status", ["PENDING", "COMPLETED", "PARTIALLY_REFUNDED", "DECLINED", ""])
def test_nonterminal_capture_stays_alertable(backend: Any, status: str) -> None:
    store, ws, balance = backend
    rows = enqueue(store)
    api = API(ws)
    api.capture["status"] = status
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=api) == 0
    assert inbox(store) == rows
    assert balance(ws) == 0


@pytest.mark.parametrize("defect", ["capture_id", "refund_id", "capture_ref", "amount", "currency", "status", "workspace"])
def test_bad_provider_proof_fails_closed(backend: Any, defect: str) -> None:
    store, ws, balance = backend
    rows = enqueue(store)
    api = API(ws)
    if defect == "capture_id":
        api.capture["id"] = "DIFFERENT"
    elif defect == "refund_id":
        api.refund["id"] = "DIFFERENT"
    elif defect == "capture_ref":
        api.refund["supplementary_data"]["related_ids"]["capture_id"] = "DIFFERENT"
    elif defect == "amount":
        api.refund["amount"]["value"] = "3.00"
    elif defect == "currency":
        api.refund["amount"]["currency_code"] = "EUR"
    elif defect == "status":
        api.refund["status"] = "FAILED"
    else:
        del api.capture["custom_id"]
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=api) == 0
    assert inbox(store) == rows
    assert balance(ws) == 0


def test_new_adverse_observation_stays_alertable_until_verified(backend: Any) -> None:
    store, ws, _ = backend
    enqueue(store)
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=API(ws)) == 5
    event = adverse("dispute", "lost", minute=5)
    store.record_adverse_trust_event(event)
    assert len(inbox(store)) == 1
    # Recheck canonical status using the retained full-refund row, rather
    # than trusting a previous receipt as proof of the provider's current state.
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=API(ws)) == 1
    assert not inbox(store)


def test_retained_refund_pointer_uses_successful_not_pending_observation(backend: Any) -> None:
    store, ws, _ = backend
    store.record_adverse_trust_event(adverse(status="pending", minute=0))
    store.record_adverse_trust_event(adverse(minute=1))
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=API(ws)) == 2
    store.record_adverse_trust_event(adverse("dispute", "lost", minute=5))
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=API(ws)) == 1
    assert not inbox(store)


def test_provider_timeout_keeps_all_alerts_and_does_not_log_response(backend: Any, caplog: Any) -> None:
    store, ws, _ = backend
    rows = enqueue(store)
    class Broken(API):
        def get(self, path: str, params: Any = None) -> dict[str, Any]:
            raise TimeoutError("secret-provider-response")
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=Broken(ws)) == 0
    assert inbox(store) == rows
    assert "secret-provider-response" not in caplog.text


def test_proof_cannot_cover_another_payment_or_modified_row(backend: Any) -> None:
    store, ws, _ = backend
    rows = enqueue(store)
    proof = RefundedCaptureProof("OTHER", ws, CHARGED, "refund:REFUND1", NOW)
    with pytest.raises(ValueError):
        record_refunded_uncredited(store, proof, rows)
    proof = dataclasses.replace(proof, capture_id="CAPTURE1")
    changed = tuple(dataclasses.replace(row, payload=row.payload + " ") for row in rows)
    assert record_refunded_uncredited(store, proof, changed) == 0
    assert inbox(store) == rows


def test_missing_credentials_does_not_query_provider() -> None:
    assert reconcile_uncredited_paypal_inbox(object(), SimpleNamespace()) == 0


def test_work_is_bounded(monkeypatch: Any) -> None:
    store = InMemoryStore()
    for index in range(10):
        store.record_adverse_trust_event(adverse(capture=f"CAPTURE{index}"))
    calls: list[Any] = []
    monkeypatch.setattr(paypal_inbox, "verify_refunded_capture", lambda api, rows, **kw: calls.append(rows))
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=API("ws")) == 0
    assert len(calls) == paypal_inbox.MAX_CAPTURES_PER_RUN


def test_tier_worker_reconciles_before_stale_alerting(monkeypatch: Any) -> None:
    store = InMemoryStore()
    workspace = store.create_workspace("owner", "worker", trial_credit_microdollars=0)
    enqueue(store)
    monkeypatch.setattr(paypal_inbox, "PayPalHistoryClient", lambda settings: API(workspace.id))
    monkeypatch.setattr(InMemoryStore, "list_trust_tier_workspace_ids", lambda self: (), raising=False)
    alerts: list[str] = []
    monkeypatch.setattr(trust_recovery, "ops_alert", lambda message, **kw: alerts.append(message))
    result = run_tier_job(store, SETTINGS, now=NOW)
    assert not result.failed
    assert not alerts
    assert not inbox(store)


def test_partial_refund_or_dispute_alone_cannot_resolve(backend: Any) -> None:
    store, ws, _ = backend
    store.record_adverse_trust_event(adverse(amount=3_000_000))
    store.record_adverse_trust_event(adverse("dispute", "lost"))
    rows = inbox(store)
    assert reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW, client=API(ws)) == 0
    assert inbox(store) == rows


def test_failed_receipt_transaction_retains_every_alert() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    ws = store.create_workspace("owner", "rollback", trial_credit_microdollars=0).id
    rows = enqueue(store)
    proof = verify_refunded_capture(API(ws), rows, now=NOW)
    assert proof is not None
    original = store._insert_entity_once_tx
    calls = 0
    def insert(conn: Any, kind: str, entity_id: str, value: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected write failure")
        return bool(original(conn, kind, entity_id, value))
    store._insert_entity_once_tx = insert
    with pytest.raises(RuntimeError, match="injected write failure"):
        record_refunded_uncredited(store, proof, rows)
    assert conn.count_entities("trust_inbox_resolution") == 0
    assert inbox(store) == rows
    conn._raw.close()


@pytest.mark.parametrize("backend_name", ["memory", "spanner"])
def test_simultaneous_credit_and_resolution_do_not_leave_spendable_credit(backend_name: str) -> None:
    if backend_name == "memory":
        store = InMemoryStore()
    else:
        store, database, _ = make_fake_store()
    ws = store.create_workspace("owner", "concurrent", trial_credit_microdollars=0).id
    rows = enqueue(store)
    proof = verify_refunded_capture(API(ws), rows, now=NOW)
    assert proof is not None
    barrier = threading.Barrier(2)
    def resolve() -> int:
        barrier.wait(timeout=5)
        return record_refunded_uncredited(store, proof, rows)
    def complete() -> bool:
        barrier.wait(timeout=5)
        return credit(store, ws)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resolved = pool.submit(resolve)
        completed = pool.submit(complete)
        assert completed.result(timeout=10)
        assert resolved.result(timeout=10) in {0, 5}
    if backend_name == "memory":
        assert store.credit_money_snapshot(ws) == (0, 0, 0)
    else:
        assert sum(int(row["total_credits"]) for (owner, _), row in
                   database.typed["tr_credit_balance"].items() if owner == ws) == 0
    assert not inbox(store)


def test_pending_captures_do_not_starve_later_captures(monkeypatch: Any) -> None:
    store = InMemoryStore()
    for index in range(10):
        store.record_adverse_trust_event(adverse(capture=f"CAPTURE{index}"))
    seen: set[str] = set()
    def verify(api: Any, rows: Any, **kw: Any) -> None:
        from trusted_router.trust_tiers import adverse_event_from_payload
        seen.add(adverse_event_from_payload(rows[0].payload).original_payment_ref)
    monkeypatch.setattr(paypal_inbox, "verify_refunded_capture", verify)
    for tick in range(4):
        reconcile_uncredited_paypal_inbox(store, SETTINGS, now=NOW + timedelta(minutes=15 * tick), client=API("ws"))
    assert len(seen) == 10
