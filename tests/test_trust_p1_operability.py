"""P1 -- slice 1d operability and money safety (items A-I).

A. the four trust CLIs are package modules invoked with ``-m``;
B. a payment fact is written only with local credit evidence in the same
   transaction, the recurring pass writes no payment facts, and the typed
   credit path credits exactly once when a scan-written fact pre-exists;
C. the history converter emits byte-for-byte what the live webhook writes;
D. ``--plan`` writes nothing and prints every consequence ``--apply`` implies;
E. the recurring pass refuses an incomplete marker;
F. the tier job takes an explicit environment and survives a raising workspace;
G. the marker migration recreates production's three-column table;
H. the Stripe account id and owner-inventory version are single pins;
I. release wiring for the trust jobs is present and gated off by default.

No sockets, no network: Stripe is a recorded-response fake, deploy scripts run
under the stub harness, and the store fakes are in-process.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    HarnessRun,
    ScriptFixture,
    summarise,
)
from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router import (
    trust_backfill_cli,
    trust_reconcile_cli,
    trust_reconcile_job,
    trust_tier_cli,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_trust import insert_credit_trust_event
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, TrustEvent
from trusted_router.storage_trust_reconciliation import (
    SpannerTrustReconciliationRepository,
    trust_reconciliation_repository,
)
from trusted_router.stripe_trust_history import (
    StripeTrustScan,
    credit_evidence_ids,
    event_adverse_status,
    latest_adverse_event,
    scan_created_range,
    scan_stripe_responses,
)
from trusted_router.trust_reconcile_job import (
    plan_historical_backfill,
    run_historical_backfill,
    run_recurring_reconciliation,
)
from trusted_router.trust_reconciliation import (
    OWNER_INVENTORY_ACCOUNT_ID,
    OWNER_INVENTORY_PROVIDER,
    OWNER_INVENTORY_SOURCE,
    OWNER_INVENTORY_SOURCE_VERSION,
    STRIPE_TRUST_SOURCE,
    STRIPE_TRUST_SOURCE_VERSION,
    BackfillMarker,
    MarkerRequirement,
    canonical_mapping,
    canonical_records_from_events,
    completed_marker_satisfies,
    reconcile_canonical_mappings,
)
from trusted_router.trust_tiers import payment_or_grant_event

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
HISTORY_START = NOW - timedelta(days=100)
CLOSED_THROUGH = NOW - timedelta(minutes=15)
SESSION_CREATED = HISTORY_START + timedelta(days=10)
# Stripe creates the PaymentIntent after the Session; the two timestamps differ
# and the live handler stamps the Session's. This is the byte P1-C is about.
PI_CREATED = SESSION_CREATED + timedelta(seconds=41)
MARKER_KWARGS: dict[str, Any] = {
    "account_id": "acct_test",
    "environment": "production",
    "source": STRIPE_TRUST_SOURCE,
    "source_version": STRIPE_TRUST_SOURCE_VERSION,
}


def _metadata(workspace_id: str, **extra: str) -> dict[str, str]:
    return {"workspace_id": workspace_id, "payment_method": "card", **extra}


def _payment_intent(
    workspace_id: str,
    *,
    payment_intent_id: str = "pi_1",
    amount: int = 500,
    created: datetime = PI_CREATED,
    **extra: str,
) -> dict[str, Any]:
    return {
        "id": payment_intent_id,
        "object": "payment_intent",
        "status": "succeeded",
        "created": int(created.timestamp()),
        "amount": amount,
        "amount_received": amount,
        "currency": "usd",
        "metadata": _metadata(workspace_id, **extra),
    }


def _checkout_session(
    workspace_id: str,
    *,
    session_id: str = "cs_1",
    payment_intent_id: str = "pi_1",
    amount: int = 500,
    created: datetime = SESSION_CREATED,
    **extra: str,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "object": "checkout.session",
        "mode": "payment",
        "payment_status": "paid",
        "payment_intent": payment_intent_id,
        "amount_total": amount,
        "currency": "usd",
        "created": int(created.timestamp()),
        "metadata": _metadata(workspace_id, **extra),
    }


def _checkout_event(session: dict[str, Any], *, event_id: str = "evt_checkout_1") -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "created": int(session["created"]) + 5,
        "data": {"object": session},
    }


def _refund_event(
    *,
    refund_id: str = "re_1",
    payment_intent_id: str = "pi_1",
    amount: int = 200,
    created: datetime = PI_CREATED + timedelta(days=2),
    event_id: str = "evt_refund_1",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "type": "refund.updated",
        "created": int(created.timestamp()),
        "data": {
            "object": {
                "id": refund_id,
                "object": "refund",
                "payment_intent": payment_intent_id,
                "amount": amount,
                "status": "succeeded",
                "created": int(created.timestamp()),
            }
        },
    }


class _FakeStripe:
    """Recorded-response Stripe client: no I/O, every list/retrieve answered."""

    def __init__(
        self,
        *,
        payment_intents: list[dict[str, Any]],
        sessions: list[dict[str, Any]] = (),  # type: ignore[assignment]
        events: list[dict[str, Any]] = (),  # type: ignore[assignment]
        refunds: list[dict[str, Any]] = (),  # type: ignore[assignment]
    ) -> None:
        self._payment_intents = list(payment_intents)
        self._sessions = list(sessions)
        self._events = list(events)
        self._refunds = list(refunds)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        stripe = self

        class PaymentIntent:
            @staticmethod
            def list(**kwargs: Any) -> list[dict[str, Any]]:
                stripe.calls.append(("PaymentIntent.list", kwargs))
                return stripe._payment_intents

            @staticmethod
            def retrieve(payment_intent_id: str) -> dict[str, Any]:
                stripe.calls.append(("PaymentIntent.retrieve", {"id": payment_intent_id}))
                return next(row for row in stripe._payment_intents if row["id"] == payment_intent_id)

        class Refund:
            @staticmethod
            def list(**kwargs: Any) -> list[dict[str, Any]]:
                stripe.calls.append(("Refund.list", kwargs))
                return stripe._refunds

        class Dispute:
            @staticmethod
            def list(**kwargs: Any) -> list[dict[str, Any]]:
                stripe.calls.append(("Dispute.list", kwargs))
                return []

        class Event:
            @staticmethod
            def list(**kwargs: Any) -> list[dict[str, Any]]:
                stripe.calls.append(("Event.list", kwargs))
                return [row for row in stripe._events if row["type"] == kwargs.get("type")]

        class Session:
            @staticmethod
            def list(**kwargs: Any) -> list[dict[str, Any]]:
                stripe.calls.append(("checkout.Session.list", kwargs))
                return [
                    row
                    for row in stripe._sessions
                    if row["payment_intent"] == kwargs.get("payment_intent")
                ]

        self.PaymentIntent = PaymentIntent
        self.Refund = Refund
        self.Dispute = Dispute
        self.Event = Event
        self.checkout = SimpleNamespace(Session=Session)


def _spanner_workspace() -> tuple[Any, Any, str]:
    store, database, _ = make_fake_store()
    workspace = store.create_workspace("owner", "p1", trial_credit_microdollars=0)
    return store, database, workspace.id


def _balance(database: Any, workspace_id: str) -> int:
    return sum(
        int(row["total_credits"])
        for (owner, _shard), row in database.typed["tr_credit_balance"].items()
        if owner == workspace_id
    )


def _facts(database: Any, workspace_id: str) -> dict[str, dict[str, Any]]:
    return {
        event_id: row
        for (owner, event_id), row in database.typed.get("tr_trust_event", {}).items()
        if owner == workspace_id
    }


def _alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        trust_reconcile_job,
        "ops_alert",
        lambda message, *, fingerprint, tags=None: calls.append((message, fingerprint)),
    )
    return calls


def _backfill(repository: Any, scan: StripeTrustScan, *, provider: str = "stripe") -> Any:
    return run_historical_backfill(
        repository,
        scan,
        provider=provider,
        history_start=HISTORY_START,
        closed_through=CLOSED_THROUGH,
        consistency_delay_seconds=900,
        now=NOW,
        **MARKER_KWARGS,
    )


# --------------------------------------------------------------------------- A


@pytest.mark.parametrize(
    "module",
    [
        "trusted_router.trust_backfill_cli",
        "trusted_router.trust_reconcile_cli",
        "trusted_router.trust_tier_cli",
        "trusted_router.owner_inventory_cli",
    ],
)
def test_trust_clis_are_package_modules_shipped_in_the_image(module: str) -> None:
    loaded = importlib.import_module(module)
    assert callable(loaded.main)
    assert Path(str(loaded.__file__)).is_relative_to(ROOT / "src" / "trusted_router")
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY src ./src" in dockerfile
    for retired in (
        "backfill_stripe_trust.py",
        "reconcile_stripe_trust.py",
        "recompute_trust_tiers.py",
        "backfill_owner_inventory.py",
    ):
        assert not (ROOT / "scripts" / retired).exists(), retired


# --------------------------------------------------------------------------- B


def test_backfill_refuses_payment_fact_without_local_credit_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts = _alerts(monkeypatch)
    store, database, workspace_id = _spanner_workspace()
    repository = trust_reconciliation_repository(store)
    scan = scan_stripe_responses(
        payment_intents=(_payment_intent(workspace_id),),
        refunds=(),
        disputes=(),
        recorded_at=NOW,
        checkout_sessions={"pi_1": _checkout_session(workspace_id)},
    )
    # A card Checkout payment has no derivable marker id: evidence is empty.
    assert scan.credit_evidence == {"pi_1": ()}

    result = _backfill(repository, scan)

    assert _facts(database, workspace_id) == {}
    assert result.uncredited_payment_refs == ("pi_1",)
    assert result.marker.unmatched_count == 1
    assert result.marker.completed_at is None
    assert result.marker.closed_through == HISTORY_START
    assert [fingerprint for _message, fingerprint in alerts] == [
        ["trust.backfill.uncredited_payment", "stripe", "pi_1"]
    ]
    assert alerts[0][0].startswith("trust.backfill.uncredited_payment provider=stripe payment_ref=pi_1")

    # The crediting webhook's marker appears (it was processed after all) and
    # the operator attests which Event credited pi_1: the same scan now writes.
    store._write_entity("stripe_event", "evt_checkout_1", {"created_at": "2026-09-05T00:00:00Z"})
    credited_scan = scan_stripe_responses(
        payment_intents=(_payment_intent(workspace_id),),
        refunds=(),
        disputes=(),
        recorded_at=NOW,
        checkout_sessions={"pi_1": _checkout_session(workspace_id)},
        crediting_events={"pi_1": ("evt_checkout_1",)},
    )
    assert repository.has_credit_evidence(credited_scan.credit_evidence["pi_1"])
    second = _backfill(repository, credited_scan)
    assert set(_facts(database, workspace_id)) == {"trust-backfill:stripe:payment:pi_1"}
    assert second.marker.is_complete and second.marker.completed_at == NOW
    assert second.uncredited_payment_refs == ()
    assert len(alerts) == 1
    # Re-running is a no-op: the existing fact is a duplicate, never a rewrite.
    assert repository.write_payment_fact(credited_scan.payments[0], evidence_ids=()) == "duplicate"


def test_derivable_evidence_ids_match_the_live_marker_keys() -> None:
    ach = _payment_intent("ws", payment_method="ach")
    assert credit_evidence_ids(
        ach, checkout_session=_checkout_session("ws", payment_method="ach")
    ) == ("stripe_checkout:pi_1", "stripe_checkout:cs_1")
    x402 = _payment_intent("ws", payment_method="x402", amount_microdollars="5000000")
    assert credit_evidence_ids(x402) == ("x402:pi_1",)
    card = _payment_intent("ws")
    assert credit_evidence_ids(card) == ()
    assert credit_evidence_ids(card, crediting_event_ids=("evt_a", "evt_a", "")) == ("evt_a",)
    auto_refill = _payment_intent("ws", auto_refill="true", amount_microdollars="5000000")
    assert credit_evidence_ids(auto_refill, crediting_event_ids=("evt_pi",)) == ("evt_pi",)


def test_postgres_writer_applies_the_same_evidence_rule() -> None:
    store = postgres_store_on(sqlite_postgres_conn())
    workspace = store.create_workspace("owner", "pg", trial_credit_microdollars=0)
    repository = trust_reconciliation_repository(store)
    fact = payment_or_grant_event(
        workspace.id,
        "trust-backfill:stripe:payment:pi_pg",
        1_000_000,
        CreditProvenance("checkout", "stripe", "pi_pg", NOW),
        recorded_at=NOW,
        payment_amount_microdollars=1_200_000,
        currency="USD",
    )
    assert repository.write_payment_fact(fact, evidence_ids=("evt_pg",)) == "uncredited"
    assert repository.list_provider_events("stripe") == ()
    store._run_transaction(
        lambda conn: store._insert_entity_once_tx(
            conn, "stripe_event", "evt_pg", {"created_at": "2026-09-05T00:00:00Z"}
        )
    )
    assert repository.has_credit_evidence(("evt_pg",))
    assert repository.write_payment_fact(fact, evidence_ids=("evt_pg",)) == "inserted"
    assert repository.write_payment_fact(fact, evidence_ids=("evt_pg",)) == "duplicate"
    assert len(repository.list_provider_events("stripe")) == 1


class _RecordingRepository:
    """Marker + adverse writer that records every payment write attempt."""

    def __init__(self, marker: BackfillMarker | None) -> None:
        self.marker = marker
        self.payment_writes: list[str] = []
        self.adverse_writes: list[str] = []
        self.saved: list[BackfillMarker] = []

    def get_marker(self, *_key: str) -> BackfillMarker | None:
        return self.marker

    def save_marker(self, marker: BackfillMarker) -> None:
        self.saved.append(marker)

    def write_payment_fact(self, event: TrustEvent, *, evidence_ids: tuple[str, ...] = ()) -> str:
        self.payment_writes.append(str(event.original_payment_ref))
        return "inserted"

    def has_credit_evidence(self, evidence_ids: tuple[str, ...]) -> bool:
        return False

    def write_adverse_fact(self, event: AdverseTrustEvent) -> str:
        self.adverse_writes.append(event.adverse_ref)
        return "inbox"

    def list_provider_events(self, provider: str) -> tuple[TrustEvent, ...]:
        return ()

    def list_outstanding(self, provider: str) -> tuple[Any, ...]:
        return ()

    def replicate_workspace_watermark(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("not used")


def _complete_marker(**overrides: Any) -> BackfillMarker:
    values: dict[str, Any] = {
        "provider": "stripe",
        "history_start": HISTORY_START,
        "closed_through": CLOSED_THROUGH,
        "consistency_delay_seconds": 900,
        "unmatched_count": 0,
        "semantic_mismatch_count": 0,
        "completed_at": NOW,
        **MARKER_KWARGS,
    }
    values.update(overrides)
    return BackfillMarker(**values)


def test_recurring_pass_writes_no_payment_facts_and_reports_a_missing_credit() -> None:
    repository = _RecordingRepository(_complete_marker())
    tail_scan = scan_stripe_responses(
        payment_intents=(_payment_intent("ws_tail", created=NOW - timedelta(hours=1)),),
        refunds=(_refund_event()["data"]["object"],),
        disputes=(),
        recorded_at=NOW + timedelta(minutes=15),
        crediting_events={"pi_1": ("evt_checkout_1",)},
    )
    assert tail_scan.payments and tail_scan.adverse

    result = run_recurring_reconciliation(
        repository,
        lambda _start, _end: tail_scan,
        lambda _row, _now: (_ for _ in ()).throw(AssertionError("no outstanding")),
        provider="stripe",
        cadence_seconds=900,
        now=NOW + timedelta(minutes=15),
        alert_horizon=lambda _row: None,
        **MARKER_KWARGS,
    )

    assert repository.payment_writes == []
    assert repository.adverse_writes == ["re_1"]
    # The uncredited tail payment is a source-only key: unmatched, fail-closed,
    # and never "fixed" by writing the fact the webhook has not written.
    assert result.marker.unmatched_count >= 1
    assert result.marker.completed_at is None
    assert result.marker.closed_through == CLOSED_THROUGH
    assert not result.watermark_advanced


def _seed_scan_fact(store: Any, database: Any, workspace_id: str, *, credited: int = 5_000_000) -> None:
    """Reproduce the pre-P1 defect: a scan wrote the fact, nobody credited it."""

    fact = payment_or_grant_event(
        workspace_id,
        "trust-backfill:stripe:payment:pi_1",
        credited,
        CreditProvenance("checkout", "stripe", "pi_1", SESSION_CREATED),
        recorded_at=NOW,
        payment_amount_microdollars=credited,
        currency="USD",
    )
    assert store._run_in_transaction(
        lambda transaction: insert_credit_trust_event(transaction, store._param_types, fact)
    )
    assert _facts(database, workspace_id).keys() == {"trust-backfill:stripe:payment:pi_1"}


def test_spanner_credit_path_credits_exactly_once_when_a_scan_fact_pre_exists() -> None:
    store, database, workspace_id = _spanner_workspace()
    _seed_scan_fact(store, database, workspace_id)
    assert _balance(database, workspace_id) == 0

    def credit(event_id: str) -> bool:
        return store.credit_workspace_typed_direct(
            workspace_id,
            5_000_000,
            event_id,
            provenance=CreditProvenance("checkout", "stripe", "pi_1", SESSION_CREATED),
            payment_amount_microdollars=5_000_000,
            currency="usd",
        )

    # The webhook Stripe retried for hours finally lands: credited, not
    # credited:false -- that answer would have stopped the retries for good.
    assert credit("evt_late_webhook") is True
    assert _balance(database, workspace_id) == 5_000_000
    # Same event again is a replay.
    assert credit("evt_late_webhook") is False
    # A second Stripe event for the same PaymentIntent is a replay too: the
    # fact's own event id now carries a marker.
    assert credit("evt_second_event_same_pi") is False
    assert _balance(database, workspace_id) == 5_000_000
    assert _facts(database, workspace_id).keys() == {"trust-backfill:stripe:payment:pi_1"}
    assert store._read_entity("stripe_event", "trust-backfill:stripe:payment:pi_1", dict) == {
        "created_at": store._read_entity("stripe_event", "evt_late_webhook", dict)["created_at"],
        "credited_by_event_id": "evt_late_webhook",
    }


def test_spanner_credit_path_still_refuses_a_second_event_for_a_live_fact() -> None:
    store, database, workspace_id = _spanner_workspace()
    provenance = CreditProvenance("checkout", "stripe", "pi_live", SESSION_CREATED)
    assert store.credit_workspace_typed_direct(
        workspace_id, 700_000, "evt_first", provenance=provenance,
        payment_amount_microdollars=700_000, currency="usd",
    )
    assert not store.credit_workspace_typed_direct(
        workspace_id, 700_000, "evt_duplicate_for_same_pi", provenance=provenance,
        payment_amount_microdollars=700_000, currency="usd",
    )
    assert _balance(database, workspace_id) == 700_000


def test_postgres_credit_path_credits_exactly_once_when_a_scan_fact_pre_exists() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = store.create_workspace("owner", "pg-credit", trial_credit_microdollars=0)
    fact = payment_or_grant_event(
        workspace.id,
        "trust-backfill:stripe:payment:pi_pg",
        2_000_000,
        CreditProvenance("checkout", "stripe", "pi_pg", SESSION_CREATED),
        recorded_at=NOW,
        payment_amount_microdollars=2_000_000,
        currency="USD",
    )
    assert store._run_transaction(
        lambda conn: store._insert_credit_trust_event_tx(
            conn,
            workspace_id=workspace.id,
            event_id=fact.event_id,
            amount_microdollars=2_000_000,
            provenance=CreditProvenance("checkout", "stripe", "pi_pg", SESSION_CREATED),
            recorded_at=NOW,
            payment_amount_microdollars=2_000_000,
            currency="USD",
        )
    )

    def credit(event_id: str) -> bool:
        return store.credit_workspace_typed_direct(
            workspace.id,
            2_000_000,
            event_id,
            provenance=CreditProvenance("checkout", "stripe", "pi_pg", SESSION_CREATED),
            payment_amount_microdollars=2_000_000,
            currency="usd",
        )

    assert credit("evt_pg_late") is True
    assert conn.balance(workspace.id)[0] == 2_000_000
    assert credit("evt_pg_late") is False
    assert credit("evt_pg_other_event") is False
    assert conn.balance(workspace.id)[0] == 2_000_000


# --------------------------------------------------------------------------- C


def _webhook_client(store: Any) -> TestClient:
    settings = Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token=None,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
    )
    client = TestClient(create_app(settings, init_observability=False))
    configure_store(store)
    return client


def test_live_checkout_and_refund_facts_reconcile_with_zero_semantic_mismatches() -> None:
    """Literal fixture through the real webhook handler vs a backfill-shaped scan.

    Session.created != PaymentIntent.created on purpose: before P1-C the
    converter stamped PI.created and a created-based watermark, and this diff
    could never reach zero.
    """

    store, database, workspace_id = _spanner_workspace()
    session = _checkout_session(workspace_id)
    payment_intent = _payment_intent(workspace_id)
    refund_event = _refund_event()
    try:
        client = _webhook_client(store)
        credited = client.post("/v1/internal/stripe/webhook", json=_checkout_event(session))
        assert credited.status_code == 200, credited.text
        assert credited.json()["data"]["credited"] is True
        refunded = client.post("/v1/internal/stripe/webhook", json=refund_event)
        assert refunded.status_code == 200, refunded.text
        assert refunded.json()["data"]["adverse"][0]["outcome"] == "applied"
    finally:
        configure_store(InMemoryStore())
    live = _facts(database, workspace_id)
    assert live["evt_checkout_1"]["provider_ordering_watermark"] is None
    assert live["evt_checkout_1"]["occurred_at"] == SESSION_CREATED

    # What scan_created_range stamps from the latest Stripe Event about re_1:
    # the same Event the live handler processed, so all three Event-derived
    # bytes (occurred_at, subtype, watermark) match.
    refund_object = dict(refund_event["data"]["object"])
    refund_object["_trust_ordering_watermark"] = f"{refund_event['created']:020d}:evt_refund_1"
    refund_object["_trust_event_created"] = refund_event["created"]
    refund_object["_trust_event_type"] = "refund.updated"
    scan = scan_stripe_responses(
        payment_intents=(payment_intent,),
        refunds=(refund_object,),
        disputes=(),
        recorded_at=NOW,
        checkout_sessions={"pi_1": session},
        crediting_events={"pi_1": ("evt_checkout_1",)},
    )
    repository = trust_reconciliation_repository(store)
    source = canonical_mapping(canonical_records_from_events(scan.source_events))
    local = canonical_mapping(canonical_records_from_events(repository.list_provider_events("stripe")))
    diff = reconcile_canonical_mappings(source, local)
    assert diff.semantic_mismatch_count == 0, diff
    assert diff.unmatched_count == 0, diff

    result = _backfill(repository, scan)
    assert result.marker.semantic_mismatch_count == 0
    assert result.marker.unmatched_count == 0
    assert result.marker.completed_at == NOW
    # Nothing was rewritten: the live facts are the only facts.
    assert set(_facts(database, workspace_id)) == {"evt_checkout_1", "evt_refund_1:re_1"}


def test_converter_resolves_the_checkout_session_and_events_through_the_client() -> None:
    session = _checkout_session("ws_c")
    stripe = _FakeStripe(
        payment_intents=[_payment_intent("ws_c")],
        sessions=[session],
        events=[_checkout_event(session)],
    )
    scan = scan_created_range(
        stripe, start=HISTORY_START, end=CLOSED_THROUGH, recorded_at=NOW,
        credited_events={"pi_1": ("evt_attested",)},
    )
    payment = scan.payments[0]
    assert payment.occurred_at == SESSION_CREATED
    assert payment.provider_ordering_watermark is None
    assert scan.credit_evidence == {"pi_1": ("evt_checkout_1", "evt_attested")}
    assert ("checkout.Session.list", {"payment_intent": "pi_1", "limit": 1}) in stripe.calls
    event_lists = [kwargs for name, kwargs in stripe.calls if name == "Event.list"]
    assert {kwargs["type"] for kwargs in event_lists} == {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "payment_intent.succeeded",
    }
    assert all("lt" not in kwargs["created"] for kwargs in event_lists)


# --------------------------------------------------------------------------- D


def test_plan_mode_lists_credit_status_and_every_adverse_consequence_without_writing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, database, workspace_id = _spanner_workspace()
    store._write_entity("stripe_event", "evt_checkout_1", {"created_at": "2026-09-05T00:00:00Z"})
    other = store.create_workspace("owner", "p1-other", trial_credit_microdollars=0).id
    refund = _refund_event(refund_id="re_plan", payment_intent_id="pi_1", amount=250)
    scan = scan_stripe_responses(
        payment_intents=(
            _payment_intent(workspace_id),
            _payment_intent(other, payment_intent_id="pi_uncredited", amount=900),
        ),
        refunds=(refund["data"]["object"],),
        disputes=(),
        recorded_at=NOW,
        checkout_sessions={"pi_1": _checkout_session(workspace_id)},
        crediting_events={"pi_1": ("evt_checkout_1",)},
    )
    repository = trust_reconciliation_repository(store)
    before = dict(database.typed.get("tr_trust_event", {})), dict(database.typed.get("tr_trust_backfill", {}))

    plan = plan_historical_backfill(repository, scan, provider="stripe")

    assert [(row.payment_ref, row.credited_locally, row.action) for row in plan.payments] == [
        ("pi_1", True, "write"),
        ("pi_uncredited", False, "refuse_uncredited"),
    ]
    assert plan.uncredited_count == 1
    (adverse,) = plan.adverse
    assert adverse.adverse_ref == "re_plan"
    assert adverse.action == "apply"
    assert adverse.latch_implied is True
    # 250 of 500 cents refunded recovers half of the 5,000,000 credited.
    assert adverse.recovery_target_micro == 2_500_000
    assert adverse.recovery_debit_micro == 2_500_000
    assert (dict(database.typed.get("tr_trust_event", {})), dict(database.typed.get("tr_trust_backfill", {}))) == before

    uncredited = trust_backfill_cli.print_plan(
        repository, scan, providers=("stripe",), history_start=HISTORY_START, closed_through=CLOSED_THROUGH
    )
    assert uncredited == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    kinds = [line["plan"] for line in lines]
    assert kinds == ["payment", "payment", "adverse", "summary"]
    assert lines[-1]["writes"] == 0
    assert lines[-1]["payments_uncredited"] == 1
    assert lines[-1]["recovery_debit_micro"] == 2_500_000
    assert lines[-1]["latches_implied"] == 1
    assert (dict(database.typed.get("tr_trust_event", {})), dict(database.typed.get("tr_trust_backfill", {}))) == before


def test_backfill_cli_requires_plan_or_apply_and_keeps_them_exclusive(
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    base = ["--account-id", "acct_test", "--history-start", "2026-05-01T00:00:00Z"]
    assert trust_backfill_cli.main(base) == 2
    assert "requires --plan (read-only) or --apply" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        trust_backfill_cli._parser().parse_args([*base, "--plan", "--apply"])
    args = trust_backfill_cli._parser().parse_args([*base, "--plan"])
    assert args.plan and not args.apply and args.environment == "production"
    allowlist = tmp_path / "credited.json"
    allowlist.write_text(json.dumps({"pi_a": ["evt_1", "evt_1", "evt_2"], "pi_b": "evt_3"}))
    assert trust_backfill_cli.load_credited_events(allowlist) == {
        "pi_a": ("evt_1", "evt_2"),
        "pi_b": ("evt_3",),
    }
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(["pi_a"]))
    with pytest.raises(ValueError, match="JSON object"):
        trust_backfill_cli.load_credited_events(bad)


# --------------------------------------------------------------------------- E


def test_recurring_pass_refuses_an_incomplete_marker() -> None:
    incomplete = _complete_marker(unmatched_count=1, completed_at=None, closed_through=HISTORY_START)
    repository = _RecordingRepository(incomplete)

    def never_scan(_start: datetime, _end: datetime) -> StripeTrustScan:
        raise AssertionError("an incomplete marker must refuse before scanning")

    with pytest.raises(RuntimeError, match="completed_at IS NULL"):
        run_recurring_reconciliation(
            repository, never_scan, lambda _row, _now: (_ for _ in ()).throw(AssertionError()),
            provider="stripe", cadence_seconds=900, now=NOW, alert_horizon=lambda _row: None,
            **MARKER_KWARGS,
        )
    with pytest.raises(RuntimeError, match="absent"):
        run_recurring_reconciliation(
            _RecordingRepository(None), never_scan,
            lambda _row, _now: (_ for _ in ()).throw(AssertionError()),
            provider="stripe", cadence_seconds=900, now=NOW, alert_horizon=lambda _row: None,
            **MARKER_KWARGS,
        )
    assert repository.saved == []


# --------------------------------------------------------------------------- F


def _tier_settings() -> SimpleNamespace:
    return SimpleNamespace(
        trust_qualifying_provider_set=frozenset({"stripe", "x402"}),
        trust_tier3_min_days=30,
        trust_tier3_min_paid_microdollars=50_000_000,
    )


def test_tier_job_passes_an_explicit_environment_and_survives_a_raising_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replicated: list[tuple[str, str]] = []
    computed: list[str] = []

    class Store:
        def list_trust_tier_workspace_ids(self) -> tuple[str, ...]:
            return ("ws-a", "ws-raises", "ws-c")

        def replicate_workspace_trust_reconciled_through(
            self, workspace_id: str, providers: frozenset[str], *, environment: str
        ) -> datetime | None:
            replicated.append((workspace_id, environment))
            return NOW

        def recompute_workspace_trust_tier(self, workspace_id: str, **kwargs: Any) -> int:
            if workspace_id == "ws-raises":
                raise RuntimeError("shard count mismatch")
            computed.append(workspace_id)
            return 1

    result = trust_tier_cli.run(Store(), _tier_settings(), now=NOW)
    assert result.attempted == 3
    assert result.failed == ("ws-raises",)
    assert result.succeeded == 2
    assert computed == ["ws-a", "ws-c"]
    assert replicated == [("ws-a", "production"), ("ws-raises", "production"), ("ws-c", "production")]

    replicated.clear()
    trust_tier_cli.run(Store(), _tier_settings(), environment="staging", now=NOW)
    assert {environment for _workspace, environment in replicated} == {"staging"}

    settings = _tier_settings()
    sentry: list[Any] = []
    monkeypatch.setattr(trust_tier_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(trust_tier_cli, "init_sentry", sentry.append)
    monkeypatch.setattr(trust_tier_cli, "create_store", lambda _settings: Store())
    assert trust_tier_cli.main(["--environment", "production"]) == 1
    # ops_alert (alert_stale_trust_inbox) reaches Sentry only after init_sentry.
    assert sentry == [settings]
    assert trust_tier_cli._TrustTierStore  # the protocol the CLI casts to still exists

    class CleanStore(Store):
        def recompute_workspace_trust_tier(self, workspace_id: str, **kwargs: Any) -> int:
            return 1

    monkeypatch.setattr(trust_tier_cli, "create_store", lambda _settings: CleanStore())
    assert trust_tier_cli.main([]) == 0


# --------------------------------------------------------------------------- G

MIGRATION = "scripts/deploy/migrate_trust_reconciliation.sh"


def _migration_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    table_exists: bool,
    key_columns: str = "",
    foreign_rows: str = "0",
) -> HarnessRun:
    responses: list[tuple[str, str]] = [
        (r"INFORMATION_SCHEMA\.INDEX_COLUMNS", key_columns),
        (r"FROM tr_trust_backfill WHERE provider", foreign_rows),
        (r"INFORMATION_SCHEMA\.TABLES", "1" if table_exists else "0"),
    ]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        MIGRATION,
        ScriptFixture(
            env={
                "GCP_PROJECT_ID": "harness-project",
                "SPANNER_INSTANCE_ID": "harness-instance",
                "SPANNER_DATABASE_ID": "harness-database",
            },
            responses=tuple(responses),
        ),
    )
    harness = DeployScriptHarness(tmp_path / f"migration-{table_exists}-{key_columns}-{foreign_rows}")
    return harness.run(MIGRATION)


def _ddls(run: HarnessRun) -> list[str]:
    return [
        argument.removeprefix("--ddl=")
        for call in run.calls
        if call[:5] == ["gcloud", "spanner", "databases", "ddl", "update"]
        for argument in call
        if argument.startswith("--ddl=")
    ]


def test_migration_creates_the_five_column_marker_table_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _migration_run(tmp_path, monkeypatch, table_exists=False)
    assert run.returncode == 0, summarise(run)
    ddls = _ddls(run)
    assert len(ddls) == 1 and ddls[0].startswith("CREATE TABLE tr_trust_backfill (")
    assert "PRIMARY KEY (provider, account_id, environment, source, source_version)" in ddls[0]


def test_migration_recreates_productions_three_column_table_when_it_holds_no_reconciliation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _migration_run(tmp_path, monkeypatch, table_exists=True, key_columns="3", foreign_rows="0")
    assert run.returncode == 0, summarise(run)
    ddls = _ddls(run)
    assert [ddl.split(" (")[0] for ddl in ddls] == ["DROP TABLE tr_trust_backfill", "CREATE TABLE tr_trust_backfill"]
    assert "PRIMARY KEY (provider, account_id, environment, source, source_version)" in ddls[1]
    assert "owner inventory backfill" in run.stdout


def test_migration_refuses_to_drop_a_three_column_table_with_reconciliation_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _migration_run(tmp_path, monkeypatch, table_exists=True, key_columns="3", foreign_rows="2")
    assert run.returncode == 1, summarise(run)
    assert _ddls(run) == []
    assert "refusing to drop reconciliation state" in run.stderr


def test_migration_is_idempotent_on_the_five_column_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _migration_run(tmp_path, monkeypatch, table_exists=True, key_columns="5")
    assert run.returncode == 0, summarise(run)
    assert _ddls(run) == []
    assert "five-column marker key, skip" in run.stdout


def test_migration_refuses_an_unreadable_primary_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _migration_run(tmp_path, monkeypatch, table_exists=True, key_columns="")
    assert run.returncode == 1, summarise(run)
    assert _ddls(run) == []


# --------------------------------------------------------------------------- H


def test_owner_inventory_marker_identity_is_pinned_and_written_from_the_constants() -> None:
    assert OWNER_INVENTORY_SOURCE_VERSION == "owner-inventory-v1"
    assert (OWNER_INVENTORY_PROVIDER, OWNER_INVENTORY_ACCOUNT_ID, OWNER_INVENTORY_SOURCE) == (
        "owner_inventory", "local", "tr_entities.workspace",
    )
    requirement = MarkerRequirement(
        OWNER_INVENTORY_PROVIDER, OWNER_INVENTORY_ACCOUNT_ID, "production",
        OWNER_INVENTORY_SOURCE, OWNER_INVENTORY_SOURCE_VERSION,
    )
    memory = InMemoryStore()
    memory.ensure_user("owner@example.com", trial_credit_microdollars=0)
    memory.backfill_owner_inventory(source_version=OWNER_INVENTORY_SOURCE_VERSION, environment="production")
    memory_marker = BackfillMarker(**memory.trust_backfills[tuple(dataclasses.asdict(requirement).values())])
    assert completed_marker_satisfies(memory_marker, requirement)

    store, _database, _workspace_id = _spanner_workspace()
    store.backfill_owner_inventory(source_version=OWNER_INVENTORY_SOURCE_VERSION, environment="production")
    spanner_marker = trust_reconciliation_repository(store).get_marker(**dataclasses.asdict(requirement))
    assert completed_marker_satisfies(spanner_marker, requirement)

    postgres = postgres_store_on(sqlite_postgres_conn())
    postgres.ensure_user("pg-owner@example.com", trial_credit_microdollars=0)
    postgres.backfill_owner_inventory(source_version=OWNER_INVENTORY_SOURCE_VERSION, environment="production")
    postgres_marker = trust_reconciliation_repository(postgres).get_marker(**dataclasses.asdict(requirement))
    assert completed_marker_satisfies(postgres_marker, requirement)

    source = (ROOT / "src/trusted_router/owner_inventory_cli.py").read_text()
    assert 'default=OWNER_INVENTORY_SOURCE_VERSION' in source
    assert 'parser.add_argument("--environment", default="production")' in source
    for writer in ("storage.py", "storage_gcp.py", "storage_postgres.py"):
        text = (ROOT / "src/trusted_router" / writer).read_text()
        assert "'tr_entities.workspace'" not in text and '"tr_entities.workspace"' not in text, writer
        assert "OWNER_INVENTORY_SOURCE" in text, writer


def test_stripe_account_id_is_a_single_pin_in_the_deploy_library() -> None:
    library = (ROOT / "scripts/deploy/_lib.sh").read_text()
    assert (
        'TR_TRUST_STRIPE_ACCOUNT_ID="${TR_TRUST_STRIPE_ACCOUNT_ID:-'
        '$(read_key_file_var TR_TRUST_STRIPE_ACCOUNT_ID STRIPE_ACCOUNT_ID)}"'
    ) in library
    assert "require_trust_stripe_account_id()" in library
    assert "https://api.stripe.com/v1/account" in library
    # The live key never goes on a command line: curl reads it from stdin config.
    assert "--config -" in library
    assert 'TR_TRUST_JOBS_DEPLOY="${TR_TRUST_JOBS_DEPLOY:-0}"' in library


_TRUST_STUB_ENV = {
    "TR_TRUST_HISTORY_START": "2026-05-01T00:00:00Z",
    "TR_TRUST_DRAIN_WINDOW_START": "2026-09-04T06:17:00Z",
}


def _trust_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    *,
    account_id: str | None,
    image_exists: bool = True,
    extra_env: dict[str, str] | None = None,
) -> HarnessRun:
    env = dict(_TRUST_STUB_ENV)
    if account_id is not None:
        env["TR_TRUST_STRIPE_ACCOUNT_ID"] = account_id
    env.update(extra_env or {})
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        script,
        ScriptFixture(
            env=env,
            failures=() if image_exists else (r"artifacts docker images describe",),
        ),
    )
    harness = DeployScriptHarness(tmp_path / Path(script).stem / str(account_id) / str(image_exists))
    return harness.run(script)


def _job_calls(run: HarnessRun, *command: str) -> list[list[str]]:
    return [
        call for call in run.calls
        if call[0] == "gcloud" and call[3 : 3 + len(command)] == list(command)
    ]


@pytest.mark.parametrize(
    "script", ["scripts/deploy/trust_reconciler.sh", "scripts/deploy/trust_backfill_job.sh"],
)
def test_trust_jobs_refuse_an_empty_stripe_account_id_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str,
) -> None:
    # No env pin, no key file, and the stubbed curl answers nothing for the
    # deployed account: the script must stop before touching Cloud Run.
    run = _trust_run(tmp_path, monkeypatch, script, account_id="")
    assert run.returncode == 1, summarise(run)
    assert "TR_TRUST_STRIPE_ACCOUNT_ID is empty" in run.stderr
    assert not _job_calls(run, "run", "jobs")
    assert any(call[0] == "curl" and "https://api.stripe.com/v1/account" in call for call in run.calls)
    assert not any("sk_" in argument for call in run.calls for argument in call)


def test_trust_job_scripts_deploy_module_invocations_with_the_pinned_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciler = _trust_run(
        tmp_path, monkeypatch, "scripts/deploy/trust_reconciler.sh", account_id="acct_harness",
    )
    assert reconciler.returncode == 0, summarise(reconciler)
    (job,) = _job_calls(reconciler, "run", "jobs", "update", "trusted-router-trust-reconciler")
    assert (
        "--args=-m,trusted_router.trust_reconcile_cli,--account-id,acct_harness,"
        "--environment,production" in job
    )
    (schedule,) = _job_calls(reconciler, "scheduler", "jobs", "update", "http", "trusted-router-trust-reconciler-15m")
    assert "--schedule=*/15 * * * *" in schedule
    assert not _job_calls(reconciler, "run", "jobs", "execute")

    backfill = _trust_run(
        tmp_path, monkeypatch, "scripts/deploy/trust_backfill_job.sh", account_id="acct_harness",
    )
    assert backfill.returncode == 0, summarise(backfill)
    (job,) = _job_calls(backfill, "run", "jobs", "update", "trusted-router-trust-backfill")
    assert (
        "--args=-m,trusted_router.trust_backfill_cli,--account-id,acct_harness,--environment,"
        "production,--history-start,2026-05-01T00:00:00Z,--drain-window-start,"
        "2026-09-04T06:17:00Z,--apply" in job
    )
    assert not _job_calls(backfill, "run", "jobs", "execute")

    tier = _trust_run(tmp_path, monkeypatch, "scripts/deploy/trust_tier_job.sh", account_id=None)
    assert tier.returncode == 0, summarise(tier)
    (job,) = _job_calls(tier, "run", "jobs", "update", "trusted-router-trust-tier")
    assert "--args=-m,trusted_router.trust_tier_cli,--environment,production" in job
    assert "--region" in job and job[job.index("--region") + 1] == "us-east4"
    (schedule,) = _job_calls(tier, "scheduler", "jobs", "update", "http", "trusted-router-trust-tier-15m")
    assert "--schedule=7,22,37,52 * * * *" in schedule
    assert "--location=us-central1" in schedule
    assert "--time-zone=UTC" in schedule
    for run in (reconciler, backfill, tier):
        # trust_backfill_job.sh spells the flag `--set-env-vars=...`; the other
        # two pass the value as the next argument. Accept both spellings.
        env = next(
            argument.removeprefix("--set-env-vars=")
            for call in _job_calls(run, "run", "jobs", "update")
            for argument in call
            if argument.startswith(("^|^", "--set-env-vars=^|^"))
        )
        assignments = env.removeprefix("^|^").split("|")
        assert "TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false" in assignments
        assert "TR_ENVIRONMENT=worker" in assignments


@pytest.mark.parametrize(
    "script",
    [
        "scripts/deploy/trust_reconciler.sh",
        "scripts/deploy/trust_backfill_job.sh",
        "scripts/deploy/trust_tier_job.sh",
    ],
)
def test_trust_job_scripts_refuse_a_missing_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str,
) -> None:
    run = _trust_run(tmp_path, monkeypatch, script, account_id="acct_harness", image_exists=False)
    assert run.returncode == 1, summarise(run)
    assert "does not exist" in run.stderr
    assert not _job_calls(run, "run", "jobs")
    assert not _job_calls(run, "scheduler", "jobs")


# --------------------------------------------------------------------------- I

TRUST_JOBS = "scripts/deploy/trust_jobs.sh"


def _trust_jobs_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> HarnessRun:
    monkeypatch.setitem(SCRIPT_FIXTURES, TRUST_JOBS, ScriptFixture(env=env))
    harness = DeployScriptHarness(tmp_path / "trust-jobs" / "-".join(sorted(env.values())))
    return harness.run(TRUST_JOBS)


def test_trust_jobs_wiring_is_gated_off_by_default_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env in ({}, {"TR_TRUST_JOBS_DEPLOY": "0"}, {"TR_TRUST_JOBS_DEPLOY": "true"}):
        run = _trust_jobs_run(tmp_path, monkeypatch, {**env, "TR_TRUST_STRIPE_ACCOUNT_ID": "acct_harness"})
        assert run.returncode == 0, summarise(run)
        assert "opt-in (TR_TRUST_JOBS_DEPLOY=1); skipping" in run.stderr
        # _lib.sh resolves the project number on source; nothing else may run.
        mutating = [call for call in run.calls if call[0] in {"gcloud", "curl"} and call[3:5] != ["projects", "describe"]]
        assert mutating == [], mutating


def test_trust_jobs_wiring_deploys_both_jobs_when_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _trust_jobs_run(
        tmp_path, monkeypatch, {"TR_TRUST_JOBS_DEPLOY": "1", "TR_TRUST_STRIPE_ACCOUNT_ID": "acct_harness"},
    )
    assert run.returncode == 0, summarise(run)
    updated_jobs = [call[6] for call in _job_calls(run, "run", "jobs", "update")]
    assert updated_jobs == ["trusted-router-trust-reconciler", "trusted-router-trust-tier"]
    schedulers = [call[7] for call in _job_calls(run, "scheduler", "jobs", "update", "http")]
    assert schedulers == ["trusted-router-trust-reconciler-15m", "trusted-router-trust-tier-15m"]
    assert not _job_calls(run, "run", "jobs", "execute")


def test_release_wiring_runs_trust_jobs_after_synthetic_and_after_spend_lease_reconciler() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    orchestrator = (ROOT / "scripts/deploy-gcp.sh").read_text()
    assert "run: bash scripts/deploy/trust_jobs.sh" in workflow
    synthetic = workflow.index("bash scripts/deploy/synthetic_image_refresh.sh")
    trust = workflow.index("run: bash scripts/deploy/trust_jobs.sh")
    data_manager = workflow.index("run: bash scripts/deploy/google_data_manager.sh")
    assert synthetic < trust < data_manager
    step = workflow[workflow.rindex("- name:", 0, trust):trust]
    assert "TR_TRUST_JOBS_DEPLOY: ${{ vars.TR_TRUST_JOBS_DEPLOY || '0' }}" in step
    assert "TR_TRUST_STRIPE_ACCOUNT_ID: ${{ vars.TR_TRUST_STRIPE_ACCOUNT_ID || '' }}" in step
    assert 'bash "${SCRIPT_DIR}/deploy/trust_jobs.sh"' in orchestrator
    assert orchestrator.index('deploy/spend_lease_reconciler.sh"') < orchestrator.index(
        'deploy/trust_jobs.sh"'
    ) < orchestrator.index('deploy/synthetic.sh"')
    jobs = (ROOT / TRUST_JOBS).read_text()
    assert 'if [ "${TR_TRUST_JOBS_DEPLOY:-0}" != "1" ]; then' in jobs
    assert 'bash "${SCRIPT_DIR}/trust_reconciler.sh"' in jobs
    assert 'bash "${SCRIPT_DIR}/trust_tier_job.sh"' in jobs
    # The runbook flips the flag in rollout.sh only; the job wrappers keep false.
    for name in ("trust_reconciler.sh", "trust_backfill_job.sh", "trust_tier_job.sh"):
        assert '"TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false"' in (ROOT / "scripts/deploy" / name).read_text()


def test_spanner_repository_reads_credit_evidence_with_snapshot_and_transaction() -> None:
    """The evidence check is one entity read per candidate, in the writer's txn."""

    store, _database, workspace_id = _spanner_workspace()
    repository = SpannerTrustReconciliationRepository(store)
    assert not repository.has_credit_evidence(())
    assert not repository.has_credit_evidence(("",))
    assert not repository.has_credit_evidence(("evt_missing",))
    store._write_entity("stripe_event", "evt_present", {"created_at": "2026-09-05T00:00:00Z"})
    assert repository.has_credit_evidence(("evt_missing", "evt_present"))
    fact = payment_or_grant_event(
        workspace_id,
        "trust-backfill:stripe:payment:pi_ev",
        1_000_000,
        CreditProvenance("checkout", "stripe", "pi_ev", SESSION_CREATED),
        recorded_at=NOW,
        payment_amount_microdollars=1_000_000,
        currency="USD",
    )
    assert repository.write_payment_fact(fact, evidence_ids=("evt_missing",)) == "uncredited"
    assert repository.write_payment_fact(fact, evidence_ids=("evt_missing", "evt_present")) == "inserted"
    with pytest.raises(ValueError, match="payment facts only"):
        repository.write_payment_fact(dataclasses.replace(fact, kind="grant", original_payment_ref=None))


# ------------------------------------------------------------ review round 2
# Findings 1-9 of the independent review of this branch. Each test below fails
# on the branch as reviewed and names the finding it closes.


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stripe_refund_object(
    *,
    refund_id: str = "re_1",
    payment_intent_id: str = "pi_1",
    amount: int = 200,
    status: str = "succeeded",
    created: datetime = PI_CREATED + timedelta(days=2),
) -> dict[str, Any]:
    return {
        "id": refund_id,
        "object": "refund",
        "payment_intent": payment_intent_id,
        "amount": amount,
        "status": status,
        "created": int(created.timestamp()),
    }


def _stripe_event(event_type: str, obj: dict[str, Any], *, event_id: str, created: datetime) -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": int(created.timestamp()),
        "data": {"object": obj},
    }


def _charge_refunded_event(
    refund: dict[str, Any], *, event_id: str, created: datetime, charge_id: str = "ch_1"
) -> dict[str, Any]:
    charge = {
        "id": charge_id,
        "object": "charge",
        "payment_intent": refund["payment_intent"],
        "amount": 500,
        "refunded": True,
        "refunds": {"object": "list", "data": [dict(refund)]},
    }
    return _stripe_event("charge.refunded", charge, event_id=event_id, created=created)


def _watermark(event: dict[str, Any]) -> str:
    return f"{int(event['created']):020d}:{event['id']}"


def _adverse_row(database: Any, workspace_id: str, event_id: str) -> tuple[str, datetime, str]:
    row = _facts(database, workspace_id)[event_id]
    return (row["provider_subtype"], row["occurred_at"], row["provider_ordering_watermark"])


# -- finding 1: a backfilled fact must be a replay for every later Stripe event


def test_backfilled_payment_fact_is_a_replay_for_a_later_distinct_crediting_event() -> None:
    """Spanner. The legacy payment was credited under evt_A (marker only, no fact).

    Before: write_payment_fact wrote the fact without a marker under its own
    id, so the typed credit path read every backfilled fact as "scan-written,
    never credited" and credited it AGAIN on the next distinct event id for the
    same PaymentIntent -- 0 -> 5,000,000 on a payment that was already paid out.
    """

    store, database, workspace_id = _spanner_workspace()
    store._write_entity("stripe_event", "evt_A", {"created_at": "2026-06-01T00:00:00Z"})
    repository = trust_reconciliation_repository(store)
    scan = scan_stripe_responses(
        payment_intents=(_payment_intent(workspace_id),),
        refunds=(),
        disputes=(),
        recorded_at=NOW,
        checkout_sessions={"pi_1": _checkout_session(workspace_id)},
        crediting_events={"pi_1": ("evt_A",)},
    )
    result = _backfill(repository, scan)
    assert result.marker.is_complete
    assert set(_facts(database, workspace_id)) == {"trust-backfill:stripe:payment:pi_1"}
    # The fact id carries the marker convention the second layer already writes.
    assert store._read_entity("stripe_event", "trust-backfill:stripe:payment:pi_1", dict) == {
        "created_at": _iso(NOW),
        "credited_by_event_id": "evt_A",
    }
    assert _balance(database, workspace_id) == 0

    def credit(event_id: str) -> bool:
        return store.credit_workspace_typed_direct(
            workspace_id,
            5_000_000,
            event_id,
            provenance=CreditProvenance("checkout", "stripe", "pi_1", SESSION_CREATED),
            payment_amount_microdollars=5_000_000,
            currency="usd",
            lifetime_topup_user_id="owner",
        )

    assert credit("evt_B_distinct_id_same_pi") is False
    assert credit("evt_C_yet_another") is False
    assert _balance(database, workspace_id) == 0
    assert set(_facts(database, workspace_id)) == {"trust-backfill:stripe:payment:pi_1"}
    assert store._read_entity("user_lifetime_topup", "owner", dict) is None
    # --plan reads the same marker: credited locally, nothing to write.
    plan = plan_historical_backfill(repository, scan, provider="stripe")
    assert [(row.credited_locally, row.action) for row in plan.payments] == [(True, "already_present")]
    # A fact whose evidence is gone (rollback deleted tr_trust_event rows, the
    # marker survived) is written again without tripping on the old marker.
    del database.typed["tr_trust_event"][(workspace_id, "trust-backfill:stripe:payment:pi_1")]
    assert repository.write_payment_fact(scan.payments[0], evidence_ids=("evt_A",)) == "inserted"
    assert credit("evt_D_after_rewrite") is False
    assert _balance(database, workspace_id) == 0


def test_postgres_backfilled_payment_fact_is_a_replay_for_a_later_distinct_crediting_event() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace = store.create_workspace("owner", "pg-replay", trial_credit_microdollars=0)
    store._run_transaction(
        lambda c: store._insert_entity_once_tx(
            c, "stripe_event", "evt_A", {"created_at": "2026-06-01T00:00:00Z"}
        )
    )
    repository = trust_reconciliation_repository(store)
    fact = payment_or_grant_event(
        workspace.id,
        "trust-backfill:stripe:payment:pi_pg",
        2_000_000,
        CreditProvenance("checkout", "stripe", "pi_pg", SESSION_CREATED),
        recorded_at=NOW,
        payment_amount_microdollars=2_000_000,
        currency="USD",
    )
    assert repository.write_payment_fact(fact, evidence_ids=("evt_missing", "evt_A")) == "inserted"
    marker = store._run_transaction(
        lambda c: store._read_entity_tx(c, "stripe_event", fact.event_id, dict)
    )
    assert marker == {
        "created_at": _iso(NOW),
        "credited_by_event_id": "evt_A",
        "workspace_id": workspace.id,
    }
    assert repository.has_credit_evidence((fact.event_id,))

    def credit(event_id: str) -> bool:
        return store.credit_workspace_typed_direct(
            workspace.id,
            2_000_000,
            event_id,
            provenance=CreditProvenance("checkout", "stripe", "pi_pg", SESSION_CREATED),
            payment_amount_microdollars=2_000_000,
            currency="usd",
        )

    assert credit("evt_B_distinct_id_same_pi") is False
    assert conn.balance(workspace.id)[0] == 0
    assert len(repository.list_provider_events("stripe")) == 1


# -- finding 6 (2): the live marker records the PaymentIntent it credited


def test_live_credit_markers_name_the_payment_intent_on_both_backends() -> None:
    store, _database, workspace_id = _spanner_workspace()
    provenance = CreditProvenance("checkout", "stripe", "pi_marker", SESSION_CREATED)
    assert store.credit_workspace_typed_direct(
        workspace_id, 700_000, "evt_marker", provenance=provenance,
        payment_amount_microdollars=700_000, currency="usd",
    )
    marker = store._read_entity("stripe_event", "evt_marker", dict)
    assert marker is not None
    assert (marker["provider"], marker["payment_intent"]) == ("stripe", "pi_marker")
    assert "created_at" in marker
    # Grants carry no external ref and keep the plain marker.
    assert store.credit_workspace_typed_direct(
        workspace_id, 1_000, "evt_grant", provenance=CreditProvenance("grant", "operator", None, NOW),
    )
    assert store._read_entity("stripe_event", "evt_grant", dict) == {
        "created_at": store._read_entity("stripe_event", "evt_grant", dict)["created_at"],
    }

    conn = sqlite_postgres_conn()
    postgres = postgres_store_on(conn)
    workspace = postgres.create_workspace("owner", "pg-marker", trial_credit_microdollars=0)
    assert postgres.credit_workspace_typed_direct(
        workspace.id, 700_000, "evt_pg_marker", provenance=provenance,
        payment_amount_microdollars=700_000, currency="usd",
    )
    pg_marker = postgres._run_transaction(
        lambda c: postgres._read_entity_tx(c, "stripe_event", "evt_pg_marker", dict)
    )
    assert (pg_marker["provider"], pg_marker["payment_intent"], pg_marker["workspace_id"]) == (
        "stripe", "pi_marker", workspace.id,
    )


# -- findings 2/7: adverse parity with several same-status Stripe Events


def test_converter_maps_every_event_type_the_live_handler_applies() -> None:
    refund = _stripe_refund_object(status="pending")
    created = PI_CREATED + timedelta(days=2)
    assert event_adverse_status(
        _charge_refunded_event(refund, event_id="evt_ch", created=created), kind="refund", adverse_ref="re_1",
    ) == "pending"
    assert event_adverse_status(
        _charge_refunded_event(refund, event_id="evt_ch", created=created), kind="refund", adverse_ref="re_other",
    ) is None
    assert event_adverse_status(
        _stripe_event("charge.refund.updated", {**refund, "status": "succeeded"}, event_id="evt_cru", created=created),
        kind="refund", adverse_ref="re_1",
    ) == "succeeded"
    assert event_adverse_status(
        _stripe_event("refund.failed", {**refund, "status": "canceled"}, event_id="evt_rf", created=created),
        kind="refund", adverse_ref="re_1",
    ) == "failed"
    dispute = {"id": "dp_1", "object": "dispute", "payment_intent": "pi_1", "amount": 500, "status": "lost"}
    assert event_adverse_status(
        _stripe_event("charge.dispute.funds_reinstated", dispute, event_id="evt_fr", created=created),
        kind="dispute", adverse_ref="dp_1",
    ) == "won"
    assert event_adverse_status(
        _stripe_event("charge.dispute.closed", dispute, event_id="evt_dc", created=created),
        kind="dispute", adverse_ref="dp_1",
    ) == "lost"
    assert event_adverse_status(
        _stripe_event("refund.updated", refund, event_id="evt_ru", created=created), kind="dispute", adverse_ref="re_1",
    ) is None

    pending = _stripe_event("refund.created", refund, event_id="evt_p", created=created)
    succeeded_1 = _stripe_event(
        "refund.updated", {**refund, "status": "succeeded"}, event_id="evt_s1", created=created + timedelta(hours=1)
    )
    succeeded_2 = _charge_refunded_event(
        {**refund, "status": "succeeded"}, event_id="evt_s2", created=created + timedelta(hours=2)
    )
    stripe = _FakeStripe(payment_intents=[], events=[succeeded_2, pending, succeeded_1])
    picked = latest_adverse_event(
        stripe, kind="refund", adverse_ref="re_1", occurred_at=created, lifecycle_status="succeeded"
    )
    assert picked is not None and (picked.event_id, picked.event_type) == ("evt_s2", "charge.refunded")
    picked = latest_adverse_event(
        stripe, kind="refund", adverse_ref="re_1", occurred_at=created, lifecycle_status="pending"
    )
    assert picked is not None and picked.event_id == "evt_p"
    # Without a status the latest Event of any status wins (legacy behaviour).
    picked = latest_adverse_event(stripe, kind="refund", adverse_ref="re_1", occurred_at=created)
    assert picked is not None and picked.event_id == "evt_s2"


def test_live_refund_with_four_same_status_events_reconciles_with_zero_semantic_mismatches() -> None:
    """Literal fixture through the real webhook: the normal Stripe card refund.

    Stripe emits charge.refunded and refund.created in the same second, then
    charge.refund.updated and refund.updated later. Before: the live writer
    kept the FIRST applied Event's stamps and the converter picked the LATEST
    of refund.* only, so the digests differed forever on every refunded PI.
    """

    store, database, workspace_id = _spanner_workspace()
    session = _checkout_session(workspace_id)
    payment_intent = _payment_intent(workspace_id)
    refunded_at = PI_CREATED + timedelta(days=2)
    refund = _stripe_refund_object(created=refunded_at)
    charge_refunded = _charge_refunded_event(refund, event_id="evt_zz_charge_refunded", created=refunded_at)
    refund_created = _stripe_event("refund.created", refund, event_id="evt_aa_refund_created", created=refunded_at)
    charge_refund_updated = _stripe_event(
        "charge.refund.updated", refund, event_id="evt_cc_charge_refund_updated",
        created=refunded_at + timedelta(hours=1),
    )
    refund_updated = _stripe_event(
        "refund.updated", refund, event_id="evt_bb_refund_updated", created=refunded_at + timedelta(hours=3),
    )
    try:
        client = _webhook_client(store)
        assert client.post("/v1/internal/stripe/webhook", json=_checkout_event(session)).json()["data"]["credited"]
        outcomes = []
        for event in (charge_refunded, refund_created, charge_refund_updated, refund_updated):
            response = client.post("/v1/internal/stripe/webhook", json=event)
            assert response.status_code == 200, response.text
            outcomes.append(response.json()["data"]["adverse"][0]["outcome"])
    finally:
        configure_store(InMemoryStore())
    assert outcomes == ["applied", "replay", "replay", "replay"]
    # Money moved once; the stamps converged on the max-watermark Event.
    assert _adverse_row(database, workspace_id, "evt_zz_charge_refunded:re_1") == (
        "refund.updated", refunded_at + timedelta(hours=3), _watermark(refund_updated),
    )
    live_before = dict(_facts(database, workspace_id))

    stripe = _FakeStripe(
        payment_intents=[payment_intent],
        sessions=[session],
        events=[_checkout_event(session), charge_refunded, refund_created, charge_refund_updated, refund_updated],
        refunds=[refund],
    )
    scan = scan_created_range(stripe, start=HISTORY_START, end=CLOSED_THROUGH, recorded_at=NOW)
    (adverse,) = scan.adverse
    assert (adverse.provider_subtype, adverse.occurred_at, adverse.provider_ordering_watermark) == (
        "refund.updated", refunded_at + timedelta(hours=3), _watermark(refund_updated),
    )
    repository = trust_reconciliation_repository(store)
    diff = reconcile_canonical_mappings(
        canonical_mapping(canonical_records_from_events(scan.source_events)),
        canonical_mapping(canonical_records_from_events(repository.list_provider_events("stripe"))),
    )
    assert diff.semantic_mismatch_count == 0, diff
    assert diff.unmatched_count == 0, diff
    result = _backfill(repository, scan)
    assert result.marker.is_complete and result.marker.completed_at == NOW
    assert _facts(database, workspace_id) == live_before


def test_out_of_order_same_status_events_converge_on_the_same_stamps() -> None:
    """Delivery order must not matter: later Event first, then the earlier one."""

    store, database, workspace_id = _spanner_workspace()
    session = _checkout_session(workspace_id)
    refunded_at = PI_CREATED + timedelta(days=2)
    refund = _stripe_refund_object(created=refunded_at)
    refund_created = _stripe_event("refund.created", refund, event_id="evt_aa_refund_created", created=refunded_at)
    refund_updated = _stripe_event(
        "refund.updated", refund, event_id="evt_bb_refund_updated", created=refunded_at + timedelta(hours=3),
    )
    try:
        client = _webhook_client(store)
        assert client.post("/v1/internal/stripe/webhook", json=_checkout_event(session)).json()["data"]["credited"]
        first = client.post("/v1/internal/stripe/webhook", json=refund_updated).json()["data"]["adverse"][0]
        second = client.post("/v1/internal/stripe/webhook", json=refund_created).json()["data"]["adverse"][0]
    finally:
        configure_store(InMemoryStore())
    assert (first["outcome"], second["outcome"]) == ("applied", "replay")
    assert _adverse_row(database, workspace_id, "evt_bb_refund_updated:re_1") == (
        "refund.updated", refunded_at + timedelta(hours=3), _watermark(refund_updated),
    )
    # 200 of 500 cents refunded recovers 40% of the 5,000,000 credited: once.
    assert _balance(database, workspace_id) == 3_000_000


def test_postgres_and_memory_writers_restamp_a_later_same_status_event_without_moving_money() -> None:
    provenance = CreditProvenance("checkout", "stripe", "pi_x", SESSION_CREATED)
    refunded_at = SESSION_CREATED + timedelta(days=2)

    def adverse(*, event_id: str, subtype: str, occurred_at: datetime) -> AdverseTrustEvent:
        return AdverseTrustEvent(
            event_id=f"{event_id}:re_x",
            provider="stripe",
            kind="refund",
            adverse_ref="re_x",
            original_payment_ref="pi_x",
            amount_micro=250 * 10_000,
            provider_subtype=subtype,
            lifecycle_status="succeeded",
            occurred_at=occurred_at,
            provider_ordering_watermark=f"{int(occurred_at.timestamp()):020d}:{event_id}",
            payload="",
        )

    early = adverse(event_id="evt_early", subtype="charge.refunded", occurred_at=refunded_at)
    late = adverse(event_id="evt_late", subtype="refund.updated", occurred_at=refunded_at + timedelta(hours=3))

    conn = sqlite_postgres_conn()
    postgres = postgres_store_on(conn)
    workspace = postgres.create_workspace("owner", "pg-restamp", trial_credit_microdollars=0)
    assert postgres.credit_workspace_typed_direct(
        workspace.id, 1_000_000, "evt_pay", provenance=provenance,
        payment_amount_microdollars=500 * 10_000, currency="usd",
    )
    assert postgres.record_adverse_trust_event(late).outcome == "applied"
    balance = conn.balance(workspace.id)[0]
    assert postgres.record_adverse_trust_event(early).outcome == "replay"
    assert postgres.record_adverse_trust_event(late).outcome == "replay"
    assert conn.balance(workspace.id)[0] == balance
    repository = trust_reconciliation_repository(postgres)
    (row,) = [r for r in repository.list_provider_events("stripe") if r.kind == "refund"]
    assert (row.event_id, row.provider_subtype, row.provider_ordering_watermark) == (
        "evt_late:re_x", "refund.updated", late.provider_ordering_watermark,
    )
    assert row.occurred_at == late.occurred_at

    memory = InMemoryStore()
    ws = memory.create_workspace("owner", "mem-restamp", trial_credit_microdollars=0)
    assert memory.credit_workspace_typed_direct(
        ws.id, 1_000_000, "evt_pay", provenance=provenance,
        payment_amount_microdollars=500 * 10_000, currency="usd",
    )
    assert memory.record_adverse_trust_event(early).outcome == "applied"
    total = memory.credit_money[ws.id].total_credits_microdollars
    assert memory.record_adverse_trust_event(late).outcome == "replay"
    assert memory.credit_money[ws.id].total_credits_microdollars == total
    stored = memory.trust_events[(ws.id, "evt_early:re_x")]
    assert (stored.provider_subtype, stored.occurred_at, stored.provider_ordering_watermark) == (
        "refund.updated", late.occurred_at, late.provider_ordering_watermark,
    )
    assert memory.record_adverse_trust_event(early).outcome == "replay"
    assert stored.provider_ordering_watermark == late.provider_ordering_watermark


# -- finding 9: local adverse facts are compared by key, never by the Event clock


def test_old_pending_refund_that_succeeds_inside_the_tail_keeps_the_tick_clean() -> None:
    """occurred_at is Event.created and moves on every status change; the tail
    scan lists refunds by the OBJECT's created. An old pending refund that
    succeeds inside this tick's window was local-only by clock alone.
    """

    store, database, workspace_id = _spanner_workspace()
    store._write_entity("stripe_event", "evt_checkout_1", {"created_at": "2026-09-05T00:00:00Z"})
    session = _checkout_session(workspace_id)
    refund_created_at = PI_CREATED + timedelta(days=3)
    pending_event = _stripe_event(
        "refund.created", _stripe_refund_object(status="pending", created=refund_created_at),
        event_id="evt_refund_pending", created=refund_created_at,
    )
    refund_object = dict(pending_event["data"]["object"])
    refund_object["_trust_ordering_watermark"] = _watermark(pending_event)
    refund_object["_trust_event_created"] = pending_event["created"]
    refund_object["_trust_event_type"] = "refund.created"
    scan = scan_stripe_responses(
        payment_intents=(_payment_intent(workspace_id),),
        refunds=(refund_object,),
        disputes=(),
        recorded_at=NOW,
        checkout_sessions={"pi_1": session},
        crediting_events={"pi_1": ("evt_checkout_1",)},
    )
    repository = trust_reconciliation_repository(store)
    backfill = _backfill(repository, scan)
    assert backfill.marker.is_complete
    assert [row.lifecycle_status for row in repository.list_outstanding("stripe")] == ["pending"]

    # The refund succeeds; Stripe's Event lands inside the next tick's tail.
    succeeded_event = _stripe_event(
        "refund.updated", _stripe_refund_object(status="succeeded", created=refund_created_at),
        event_id="evt_refund_succeeded", created=NOW - timedelta(minutes=5),
    )
    try:
        client = _webhook_client(store)
        applied = client.post("/v1/internal/stripe/webhook", json=succeeded_event)
        assert applied.json()["data"]["adverse"][0]["outcome"] == "applied"
    finally:
        configure_store(InMemoryStore())
    live = _facts(database, workspace_id)["trust-backfill:stripe:refund:re_1"]
    assert live["occurred_at"] == NOW - timedelta(minutes=5)
    assert repository.list_outstanding("stripe") == ()

    tick = run_recurring_reconciliation(
        repository,
        lambda _start, _end: StripeTrustScan((), (), (), (), ()),
        lambda _row, _now: (_ for _ in ()).throw(AssertionError("nothing outstanding")),
        provider="stripe",
        cadence_seconds=900,
        now=NOW + timedelta(minutes=15),
        alert_horizon=lambda _row: None,
        **MARKER_KWARGS,
    )
    assert tick.marker.unmatched_count == 0 and tick.marker.semantic_mismatch_count == 0
    assert tick.marker.is_complete and tick.watermark_advanced and tick.marker_saved
    assert tick.marker.closed_through == NOW


# -- findings 3/5/8: the recurring pass never persists an unclean marker


def test_unclean_recurring_tick_reports_but_does_not_overwrite_the_clean_marker() -> None:
    repository = _RecordingRepository(_complete_marker())
    tail_scan = scan_stripe_responses(
        payment_intents=(_payment_intent("ws_tail", created=NOW - timedelta(hours=1)),),
        refunds=(),
        disputes=(),
        recorded_at=NOW + timedelta(minutes=15),
    )
    kwargs: dict[str, Any] = dict(
        provider="stripe", cadence_seconds=900, now=NOW + timedelta(minutes=15),
        alert_horizon=lambda _row: None, **MARKER_KWARGS,
    )
    refetch = lambda _row, _now: (_ for _ in ()).throw(AssertionError("no outstanding"))  # noqa: E731
    unclean = run_recurring_reconciliation(repository, lambda _s, _e: tail_scan, refetch, **kwargs)
    assert unclean.marker.unmatched_count == 1 and unclean.marker.completed_at is None
    assert not unclean.marker_saved and not unclean.watermark_advanced
    assert repository.saved == []
    # The next tick against the same marker runs (no P1-E refusal) and, clean,
    # persists an advanced marker.
    clean = run_recurring_reconciliation(
        repository, lambda _s, _e: StripeTrustScan((), (), (), (), ()), refetch, **kwargs,
    )
    assert clean.marker_saved and clean.watermark_advanced
    assert [marker.closed_through for marker in repository.saved] == [NOW]
    # The historical backfill still persists its unclean state (P1-E relies on it).
    unclean_backfill = _RecordingRepository(None)
    result = run_historical_backfill(
        unclean_backfill,
        StripeTrustScan((), (), (), (), ("re_orphan",)),
        provider="stripe", history_start=HISTORY_START, closed_through=CLOSED_THROUGH,
        consistency_delay_seconds=900, now=NOW, **MARKER_KWARGS,
    )
    assert result.marker_saved and not result.marker.is_complete
    assert [marker.closed_through for marker in unclean_backfill.saved] == [HISTORY_START]


class _ProviderRepository(_RecordingRepository):
    def __init__(self, markers: dict[str, BackfillMarker | None]) -> None:
        super().__init__(None)
        self.markers_by_provider = markers

    def get_marker(self, provider: str, *_key: str) -> BackfillMarker | None:
        return self.markers_by_provider.get(provider)


def _reconcile_cli_fixture(
    monkeypatch: pytest.MonkeyPatch, repository: Any
) -> tuple[Any, list[Any], list[dict[str, Any]], list[tuple[str, list[str]]]]:
    settings = SimpleNamespace(stripe_secret_key=None, trust_reconcile_interval_seconds=900)
    sentry: list[Any] = []
    configured: list[dict[str, Any]] = []
    alerts: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(trust_reconcile_cli, "Settings", lambda: settings)
    monkeypatch.setattr(trust_reconcile_cli, "init_sentry", sentry.append)
    monkeypatch.setattr(trust_reconcile_cli.logging, "basicConfig", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(trust_reconcile_cli, "create_store", lambda _settings: object())
    monkeypatch.setattr(trust_reconcile_cli, "trust_reconciliation_repository", lambda _store: repository)
    monkeypatch.setattr(
        trust_reconcile_cli, "ops_alert",
        lambda message, *, fingerprint, tags=None: alerts.append((message, fingerprint)),
    )
    return settings, sentry, configured, alerts


def test_reconcile_cli_alerts_a_refused_provider_and_still_reconciles_the_other(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _ProviderRepository({
        "stripe": _complete_marker(unmatched_count=1, completed_at=None, closed_through=HISTORY_START),
        "x402": _complete_marker(provider="x402"),
    })
    settings, sentry, configured, alerts = _reconcile_cli_fixture(monkeypatch, repository)
    with caplog.at_level(logging.INFO, logger="trusted_router.trust_reconcile_job"):
        code = trust_reconcile_cli.main(
            ["--account-id", "acct_test", "--now", (NOW + timedelta(minutes=15)).isoformat()],
            stripe_client=_FakeStripe(payment_intents=[]),
        )
    assert code == 1
    # Finding 4: root logger at INFO and Sentry initialised before any alert.
    assert configured == [{"level": logging.INFO}]
    assert sentry == [settings]
    # Finding 5: the refused provider pages with the remedy, no traceback, and
    # x402 is still reconciled and persisted.
    assert [fingerprint for _message, fingerprint in alerts] == [["trust.reconcile.refused", "stripe"]]
    message = alerts[0][0]
    assert message.startswith("trust.reconcile.refused provider=stripe reason=historical trust marker is incomplete")
    assert "trusted-router-trust-backfill" in message and "runbook step 6" in message
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["marker"]["provider"] for line in printed] == ["x402"]
    assert [marker.provider for marker in repository.saved] == ["x402"]
    assert repository.saved[0].is_complete
    # Runbook steps 6 and 8 grep the job log for exactly this line.
    assert "trust.reconcile.outstanding provider=x402 value=0" in caplog.text
    assert "trust.backfill.unmatched provider=x402 value=0 semantic_mismatch=0" in caplog.text


def test_reconcile_cli_exits_one_and_alerts_on_an_unclean_tick_without_a_marker_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _ProviderRepository({"stripe": _complete_marker()})
    _settings, _sentry, _configured, alerts = _reconcile_cli_fixture(monkeypatch, repository)
    stripe = _FakeStripe(payment_intents=[_payment_intent("ws_tail", created=NOW - timedelta(hours=1))])
    code = trust_reconcile_cli.main(
        ["--account-id", "acct_test", "--providers", "stripe", "--now", (NOW + timedelta(minutes=15)).isoformat()],
        stripe_client=stripe,
    )
    assert code == 1
    assert [fingerprint for _message, fingerprint in alerts] == [["trust.backfill.unmatched", "stripe"]]
    assert repository.saved == []
    (printed,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert printed["marker_saved"] is False and printed["marker"]["unmatched_count"] == 1


def test_backfill_cli_initialises_logging_and_sentry_before_scanning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    store, _database, _workspace_id = _spanner_workspace()
    settings = SimpleNamespace(stripe_secret_key=None)
    sentry: list[Any] = []
    configured: list[dict[str, Any]] = []
    monkeypatch.setattr(trust_backfill_cli, "Settings", lambda: settings)
    monkeypatch.setattr(trust_backfill_cli, "init_sentry", sentry.append)
    monkeypatch.setattr(trust_backfill_cli.logging, "basicConfig", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(trust_backfill_cli, "create_store", lambda _settings: store)
    code = trust_backfill_cli.main(
        ["--account-id", "acct_test", "--history-start", HISTORY_START.isoformat(), "--plan"],
        stripe_client=_FakeStripe(payment_intents=[]),
    )
    assert code == 0
    assert sentry == [settings]
    assert configured == [{"level": logging.INFO}]
    summaries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["plan"] for line in summaries] == ["summary", "summary"]


# -- finding 6 (1): the allowlist is deliverable to the Cloud Run job


def test_backfill_job_mounts_the_credited_events_allowlist_only_when_a_secret_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _trust_run(tmp_path / "plain", monkeypatch, "scripts/deploy/trust_backfill_job.sh", account_id="acct_harness")
    assert plain.returncode == 0, summarise(plain)
    (job,) = _job_calls(plain, "run", "jobs", "update", "trusted-router-trust-backfill")
    assert not any("credited-events" in argument for argument in job)
    assert (
        "--update-secrets=TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest,"
        "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest" in job
    )

    mounted = _trust_run(
        tmp_path / "mounted", monkeypatch, "scripts/deploy/trust_backfill_job.sh", account_id="acct_harness",
        extra_env={"TR_TRUST_CREDITED_EVENTS_SECRET": "trustedrouter-trust-credited-events"},
    )
    assert mounted.returncode == 0, summarise(mounted)
    (job,) = _job_calls(mounted, "run", "jobs", "update", "trusted-router-trust-backfill")
    assert (
        "--args=-m,trusted_router.trust_backfill_cli,--account-id,acct_harness,--environment,"
        "production,--history-start,2026-05-01T00:00:00Z,--drain-window-start,"
        "2026-09-04T06:17:00Z,--apply,--credited-events,/etc/trust/credited-events.json" in job
    )
    assert (
        "--update-secrets=TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest,"
        "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest,"
        "/etc/trust/credited-events.json=trustedrouter-trust-credited-events:latest" in job
    )
    # gcloud refuses --set-secrets next to --update-secrets; one flag carries both.
    assert not any(argument.startswith("--set-secrets") for argument in job)
    assert "mounting credited-events allowlist secret" in mounted.stderr + mounted.stdout
