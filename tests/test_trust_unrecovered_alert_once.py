"""An unchanged unrecovered principal pages once, not on every reconcile pass.

Sentry QUILL-ROUTER-HS fired four times on 2026-11-21 for one workspace and
one unchanged 4.79 USD: trust_reconcile_cli re-observes every adverse fact on
each pass, the store answers ``replay`` with the payment's standing
unrecovered amount, and that echo re-raised the alert. Only a transition that
moved money may page. No balance or ledger behaviour changes here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.services import trust_recovery
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance

NOW = dt.datetime(2026, 11, 21, 0, 45, tzinfo=dt.UTC)


def _dispute(status: str = "lost", watermark: str = "00000000000000000001:evt_1") -> AdverseTrustEvent:
    return AdverseTrustEvent(
        event_id="evt:dp_once",
        provider="stripe",
        kind="dispute",
        adverse_ref="dp_once",
        original_payment_ref="pi_once",
        amount_micro=1_000,
        provider_subtype="charge.dispute.created",
        lifecycle_status=status,
        occurred_at=NOW,
        provider_ordering_watermark=watermark,
        payload="{}",
    )


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    def capture(message: str, **_kwargs: Any) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(trust_recovery, "ops_alert", capture)
    return sent


def _spanner_store() -> tuple[Any, str]:
    store, _database = make_fake_store()
    workspace = store.create_workspace("owner", "once", trial_credit_microdollars=0)
    # The whole credit is spent, so the dispute cannot be recovered.
    assert store.credit_workspace_typed_direct(
        workspace.id,
        1_000,
        "evt_payment_once",
        provenance=CreditProvenance("checkout", "stripe", "pi_once", NOW),
        payment_amount_microdollars=1_000,
        currency="usd",
    )
    for (owner, _), row in _database.typed["tr_credit_balance"].items():
        if owner == workspace.id:
            row["total_usage"] = row["total_credits"]
    return store, workspace.id


def test_replayed_adverse_fact_does_not_re_alert(alerts: list[str]) -> None:
    store, workspace_id = _spanner_store()
    first = store.record_adverse_trust_event(_dispute())
    assert first.outcome == "applied"
    assert first.unrecovered_micro == 1_000
    assert len(alerts) == 1
    assert workspace_id in alerts[0]

    # Every reconcile pass re-observes the same fact.
    for _ in range(3):
        again = store.record_adverse_trust_event(_dispute())
        assert again.outcome == "replay"
        assert again.unrecovered_micro == 1_000
    assert len(alerts) == 1


def test_in_memory_store_replay_does_not_re_alert(alerts: list[str]) -> None:
    store = InMemoryStore()
    workspace = store.create_workspace("owner", "once", trial_credit_microdollars=0)
    assert store.credit_workspace_typed_direct(
        workspace.id,
        1_000,
        "evt_payment_once",
        provenance=CreditProvenance("checkout", "stripe", "pi_once", NOW),
        payment_amount_microdollars=1_000,
        currency="usd",
    )
    store.credit_money[workspace.id].total_usage_microdollars = 1_000
    assert store.record_adverse_trust_event(_dispute()).outcome == "applied"
    assert store.record_adverse_trust_event(_dispute()).outcome == "replay"
    assert len(alerts) == 1


def test_paypal_webhook_alerts_once_per_applied_transition(
    alerts: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The store alerts after its transaction commits; the PayPal webhook used
    # to alert again on the same result, so every applied fact paged twice.
    from tests.test_provider_trust_slice1b import funded, paypal_event
    from trusted_router import storage
    from trusted_router.services.paypal_trust import apply_paypal_adverse

    store, database, workspace = funded("paypal")
    for (owner, _), row in database.typed["tr_credit_balance"].items():
        if owner == workspace:
            row["total_usage"] = row["total_credits"]
    monkeypatch.setattr(storage, "STORE", store)
    payload = paypal_event("PAYMENT.CAPTURE.REVERSED")

    assert apply_paypal_adverse(payload) == ["applied"]
    assert len(alerts) == 1
    assert apply_paypal_adverse(payload) == ["replay"]
    assert len(alerts) == 1
