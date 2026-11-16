"""A closed Stripe inquiry claims no credited principal.

Stripe's ``warning_*`` dispute statuses are the inquiry stage: the issuer asks
for information and no funds are withdrawn. ``warning_closed`` means the
inquiry closed without ever becoming a chargeback, so nothing was taken back
and nothing is owed. Both the live webhook and the history converter record it
as lifecycle ``closed``; ``closed`` must therefore not claim the payment's
credits, while a chargeback (``succeeded``) and a lost one (``lost``) still
claim all of them and a won one claims nothing.

Rows stored as ``closed`` before this rule already hold a full claim. They keep
it (no silent re-credit on the next event for that payment) and the history
reconciliation reports them, so an operator decides what happens to them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from tests.test_trust_p1_operability import (
    PI_CREATED,
    _backfill,
    _checkout_event,
    _checkout_session,
    _payment_intent,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal.webhook import _stripe_adverse_events
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_models import CreditProvenance, TrustEvent
from trusted_router.storage_trust_reconciliation import trust_reconciliation_repository
from trusted_router.stripe_trust_history import _dispute_status, scan_stripe_responses
from trusted_router.trust_reconcile_job import plan_historical_backfill
from trusted_router.trust_reconciliation import (
    _derived_targets,
    canonical_mapping,
    canonical_records_from_events,
    reconcile_canonical_mappings,
)
from trusted_router.trust_tiers import payment_recovery_target

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
PAYMENT_INTENT = "pi_inquiry"
CREDITED = 1_000_000
CHARGED = 1_200_000
WEBHOOK = "/v1/internal/stripe/webhook"


@dataclasses.dataclass
class _Backend:
    name: str
    store: Any
    database: Any = None
    conn: Any = None

    def balance(self, workspace_id: str) -> int:
        if self.name == "memory":
            snapshot = self.store.credit_money_snapshot(workspace_id)
            assert snapshot is not None
            return int(snapshot[0])
        if self.name == "spanner":
            return sum(
                int(row["total_credits"])
                for (owner, _shard), row in self.database.typed["tr_credit_balance"].items()
                if owner == workspace_id
            )
        row = self.conn.execute(
            "SELECT SUM(total_credits) FROM tr_credit_balance WHERE workspace_id = %s",
            (workspace_id,),
        ).fetchone()
        return int(row[0])

    def payment_recovery(self, workspace_id: str) -> tuple[int, int, int]:
        """(recovery_target, recovered_micro, unrecovered_micro) of the payment fact."""

        if self.name == "memory":
            payment = next(
                row
                for row in self.store.trust_events.values()
                if row.workspace_id == workspace_id and row.kind == "payment"
            )
            values = (payment.recovery_target, payment.recovered_micro, payment.unrecovered_micro)
        elif self.name == "spanner":
            payment = next(
                row
                for (owner, _), row in self.database.typed["tr_trust_event"].items()
                if owner == workspace_id and row["kind"] == "payment"
            )
            values = (
                payment["recovery_target"],
                payment["recovered_micro"],
                payment["unrecovered_micro"],
            )
        else:
            values = self.conn.execute(
                "SELECT recovery_target, recovered_micro, unrecovered_micro "
                "FROM tr_trust_event WHERE workspace_id = %s AND kind = 'payment'",
                (workspace_id,),
            ).fetchone()
        return tuple(int(value or 0) for value in values)  # type: ignore[return-value]

    def store_legacy_status(self, workspace_id: str, adverse_ref: str, status: str) -> None:
        """Rewrite one stored dispute row's status, leaving its money columns.

        Used to reproduce what the previous mapping stored for a closed
        inquiry: lifecycle ``closed`` on a row that already holds the full
        claim, with the payment fully debited.
        """

        if self.name == "memory":
            rows = [
                row
                for row in self.store.trust_events.values()
                if row.workspace_id == workspace_id and row.adverse_ref == adverse_ref
            ]
            assert len(rows) == 1
            rows[0].lifecycle_status = status
        elif self.name == "spanner":
            rows = [
                row
                for (owner, _), row in self.database.typed["tr_trust_event"].items()
                if owner == workspace_id and row["adverse_ref"] == adverse_ref
            ]
            assert len(rows) == 1
            rows[0]["lifecycle_status"] = status
        else:
            changed = self.conn.execute(
                "UPDATE tr_trust_event SET lifecycle_status = %s "
                "WHERE workspace_id = %s AND adverse_ref = %s",
                (status, workspace_id, adverse_ref),
            )
            assert changed.rowcount == 1


def _make_backend(name: str) -> _Backend:
    if name == "memory":
        return _Backend(name, InMemoryStore())
    if name == "spanner":
        store, database = make_fake_store()
        return _Backend(name, store, database=database)
    conn = sqlite_postgres_conn(check_same_thread=False)
    return _Backend(name, postgres_store_on(conn), conn=conn)


def _client() -> TestClient:
    settings = Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token=None,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
    )
    return TestClient(create_app(settings, configure_store_arg=False, init_observability=False))


@pytest.fixture(params=["memory", "spanner", "postgres"])
def backend(request: pytest.FixtureRequest, nonreservation_stripe_payment: None) -> Iterator[_Backend]:
    selected = _make_backend(request.param)
    configure_store(selected.store)
    try:
        yield selected
    finally:
        configure_store(InMemoryStore())


def _paid_workspace(backend: _Backend) -> str:
    workspace = backend.store.create_workspace("owner", "inquiry", trial_credit_microdollars=0)
    assert backend.store.credit_workspace_typed_direct(
        workspace.id,
        CREDITED,
        "evt_inquiry_payment",
        provenance=CreditProvenance("checkout", "stripe", PAYMENT_INTENT, NOW),
        payment_amount_microdollars=CHARGED,
        currency="usd",
    )
    assert backend.balance(workspace.id) == CREDITED
    return str(workspace.id)


def _dispute_event(
    event_id: str,
    status: str,
    *,
    seconds: int,
    event_type: str = "charge.dispute.updated",
    dispute_id: str = "dp_inquiry",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": int(NOW.timestamp()) + seconds,
        "data": {
            "object": {
                "id": dispute_id,
                "object": "dispute",
                "payment_intent": PAYMENT_INTENT,
                "amount": 120,
                "status": status,
            }
        },
    }


def _refund_event(event_id: str, *, seconds: int, amount: int = 60) -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "type": "refund.updated",
        "created": int(NOW.timestamp()) + seconds,
        "data": {
            "object": {
                "id": "re_inquiry",
                "object": "refund",
                "payment_intent": PAYMENT_INTENT,
                "amount": amount,
                "status": "succeeded",
            }
        },
    }


def _post(client: TestClient, event: dict[str, Any]) -> dict[str, Any]:
    response = client.post(WEBHOOK, json=event)
    assert response.status_code == 200, response.text
    (adverse,) = response.json()["data"]["adverse"]
    return dict(adverse)


def test_closed_inquiry_claims_nothing_through_the_live_webhook(backend: _Backend) -> None:
    workspace_id = _paid_workspace(backend)
    client = _client()

    opened = _post(
        client,
        _dispute_event(
            "evt_inquiry_opened", "warning_needs_response", seconds=1,
            event_type="charge.dispute.created",
        ),
    )
    assert (opened["status"], opened["outcome"], opened["recovery_target"]) == (
        "pending", "applied", 0,
    )
    assert backend.balance(workspace_id) == CREDITED

    closed = _post(
        client,
        _dispute_event(
            "evt_inquiry_closed", "warning_closed", seconds=2,
            event_type="charge.dispute.closed",
        ),
    )

    assert (closed["status"], closed["outcome"]) == ("closed", "applied")
    assert (closed["recovery_target"], closed["unrecovered_micro"]) == (0, 0)
    assert backend.balance(workspace_id) == CREDITED
    assert backend.payment_recovery(workspace_id) == (0, 0, 0)


def test_inquiry_first_seen_closed_claims_nothing(backend: _Backend) -> None:
    workspace_id = _paid_workspace(backend)

    closed = _post(
        _client(),
        _dispute_event(
            "evt_inquiry_closed", "warning_closed", seconds=2,
            event_type="charge.dispute.closed",
        ),
    )

    assert (closed["status"], closed["outcome"], closed["recovery_target"]) == (
        "closed", "applied", 0,
    )
    assert backend.balance(workspace_id) == CREDITED


def test_refund_during_an_inquiry_keeps_only_the_refund_claim(backend: _Backend) -> None:
    workspace_id = _paid_workspace(backend)
    client = _client()
    _post(client, _dispute_event("evt_inquiry_opened", "warning_under_review", seconds=1))

    refunded = _post(client, _refund_event("evt_inquiry_refund", seconds=2))
    assert refunded["recovery_target"] == 500_000
    closed = _post(
        client,
        _dispute_event(
            "evt_inquiry_closed", "warning_closed", seconds=3,
            event_type="charge.dispute.closed",
        ),
    )

    assert (closed["outcome"], closed["recovery_target"]) == ("applied", 500_000)
    assert backend.balance(workspace_id) == 500_000
    assert backend.payment_recovery(workspace_id) == (500_000, 500_000, 0)


def test_lost_chargeback_still_claims_everything(backend: _Backend) -> None:
    workspace_id = _paid_workspace(backend)
    client = _client()

    opened = _post(
        client,
        _dispute_event(
            "evt_chargeback", "needs_response", seconds=1,
            event_type="charge.dispute.created",
        ),
    )
    assert (opened["status"], opened["recovery_target"]) == ("succeeded", CREDITED)
    assert backend.balance(workspace_id) == 0
    lost = _post(
        client,
        _dispute_event(
            "evt_chargeback_lost", "lost", seconds=2,
            event_type="charge.dispute.closed",
        ),
    )

    assert (lost["status"], lost["outcome"], lost["recovery_target"]) == (
        "lost", "applied", CREDITED,
    )
    # The graph allows lost -> closed; the closed row keeps the claim it held.
    closed = _post(client, _dispute_event("evt_chargeback_closed", "closed", seconds=3))

    assert (closed["status"], closed["outcome"], closed["recovery_target"]) == (
        "closed", "applied", CREDITED,
    )
    assert backend.balance(workspace_id) == 0
    assert backend.payment_recovery(workspace_id) == (CREDITED, CREDITED, 0)


def test_won_chargeback_still_restores_everything(backend: _Backend) -> None:
    workspace_id = _paid_workspace(backend)
    client = _client()
    _post(
        client,
        _dispute_event(
            "evt_chargeback", "under_review", seconds=1,
            event_type="charge.dispute.created",
        ),
    )
    assert backend.balance(workspace_id) == 0

    won = _post(
        client,
        _dispute_event(
            "evt_chargeback_won", "won", seconds=2,
            event_type="charge.dispute.closed",
        ),
    )

    assert (won["status"], won["recovery_target"]) == ("won", 0)
    assert backend.balance(workspace_id) == CREDITED


def test_stored_closed_row_that_already_claimed_keeps_its_claim(backend: _Backend) -> None:
    """A ``closed`` row written by the previous mapping holds the full claim.

    Nothing re-credits it implicitly: not a redelivery of the closing Event,
    and not a later adverse event on the same payment that recomputes the
    target. Releasing that claim is an operator decision.
    """

    workspace_id = _paid_workspace(backend)
    client = _client()
    _post(
        client,
        _dispute_event(
            "evt_legacy", "needs_response", seconds=1,
            event_type="charge.dispute.created",
        ),
    )
    backend.store_legacy_status(workspace_id, "dp_inquiry", "closed")
    assert backend.balance(workspace_id) == 0

    redelivered = _post(
        client,
        _dispute_event(
            "evt_legacy_closed", "warning_closed", seconds=2,
            event_type="charge.dispute.closed",
        ),
    )
    assert (redelivered["outcome"], redelivered["recovery_target"]) == ("replay", CREDITED)
    refunded = _post(client, _refund_event("evt_legacy_refund", seconds=3))

    assert (refunded["outcome"], refunded["recovery_target"]) == ("applied", CREDITED)
    assert backend.balance(workspace_id) == 0
    assert backend.payment_recovery(workspace_id) == (CREDITED, CREDITED, 0)


STRIPE_DISPUTE_STATUSES = (
    "warning_needs_response",
    "warning_under_review",
    "warning_closed",
    "needs_response",
    "under_review",
    "appeal_needs_response",
    "appeal_under_review",
    "won",
    "lost",
    "prevented",
    # Not a current Dispute status; both paths read it as an inquiry closure.
    # After a chargeback stage it keeps that claim (see the formula test).
    "closed",
    "",
)


@pytest.mark.parametrize("status", STRIPE_DISPUTE_STATUSES)
def test_live_webhook_and_history_converter_map_every_dispute_status_alike(status: str) -> None:
    event = _dispute_event("evt_status", status, seconds=1, event_type="charge.dispute.updated")
    (live,) = _stripe_adverse_events(event, event_id="evt_status")

    assert live.lifecycle_status == _dispute_status(status)
    expected = {
        "warning_needs_response": "pending",
        "warning_under_review": "pending",
        "warning_closed": "closed",
        "closed": "closed",
        "won": "won",
        "lost": "lost",
    }.get(status, "succeeded")
    assert live.lifecycle_status == expected


def _fact(
    kind: str,
    status: str,
    *,
    amount: int = 0,
    recovery_target: int | None = None,
    adverse_ref: str = "dp_1",
) -> TrustEvent:
    return TrustEvent(
        workspace_id="ws_formula",
        event_id=f"evt_{kind}_{adverse_ref}",
        kind=kind,
        provider="stripe",
        amount_micro=amount,
        original_payment_ref=PAYMENT_INTENT,
        adverse_ref=None if kind == "payment" else adverse_ref,
        occurred_at=NOW,
        recorded_at=NOW,
        payment_amount_micro=CHARGED,
        currency="USD",
        credited_micro=CREDITED,
        recovered_micro=None,
        provider_subtype="checkout" if kind == "payment" else "charge.dispute.closed",
        lifecycle_status=status,
        cumulative_refunded=None,
        recovery_target=recovery_target,
        debit_status=None,
        unrecovered_micro=None,
        provider_ordering_watermark=None,
    )


REFUND = _fact("refund", "succeeded", amount=600_000, adverse_ref="re_1")


@pytest.mark.parametrize(
    ("adverse", "expected"),
    [
        # A closed inquiry claims nothing, with or without a stored target.
        ((_fact("dispute", "closed"),), 0),
        ((_fact("dispute", "closed", recovery_target=0),), 0),
        ((_fact("dispute", "closed"), REFUND), 500_000),
        # A closed row that already held the full claim keeps it.
        ((_fact("dispute", "closed", recovery_target=CREDITED),), CREDITED),
        # Unchanged: chargeback, lost and won, refunds, horizon terminal.
        ((_fact("dispute", "pending"),), 0),
        ((_fact("dispute", "succeeded"),), CREDITED),
        ((_fact("dispute", "lost"),), CREDITED),
        ((_fact("dispute", "won"), REFUND), 500_000),
        ((REFUND,), 500_000),
        ((_fact("dispute", "terminal_by_horizon"),), 0),
        ((_fact("dispute", "terminal_by_horizon", recovery_target=CREDITED),), CREDITED),
    ],
)
def test_live_and_reconciliation_recovery_formulas_agree(
    adverse: tuple[TrustEvent, ...], expected: int
) -> None:
    payment = _fact("payment", "succeeded", amount=CHARGED)

    live_target, _ = payment_recovery_target(payment, adverse)
    derived = _derived_targets([payment, *adverse])[("stripe", PAYMENT_INTENT)]

    assert live_target == derived == expected


# ------------------------------------------------------- history reconciliation


def _history_dispute(status: str, event: dict[str, Any]) -> dict[str, Any]:
    """The dispute as ``scan_created_range`` stamps it from its latest Event."""

    dispute = dict(event["data"]["object"])
    dispute.update(
        status=status,
        created=event["created"],
        _trust_ordering_watermark=f"{event['created']:020d}:{event['id']}",
        _trust_event_created=event["created"],
        _trust_event_type=event["type"],
    )
    return dispute


def _checkout_workspace(selected: _Backend) -> tuple[str, dict[str, Any], TestClient]:
    workspace = selected.store.create_workspace("owner", "inquiry", trial_credit_microdollars=0)
    session = _checkout_session(workspace.id, payment_intent_id=PAYMENT_INTENT)
    client = _client()
    credited = client.post(WEBHOOK, json=_checkout_event(session))
    assert credited.status_code == 200, credited.text
    assert credited.json()["data"]["credited"] is True
    return str(workspace.id), session, client


def _inquiry_event(event_id: str, status: str, *, days: int, event_type: str) -> dict[str, Any]:
    event = _dispute_event(event_id, status, seconds=0, event_type=event_type)
    event["created"] = int((PI_CREATED + timedelta(days=days)).timestamp())
    event["data"]["object"]["amount"] = 500
    return event


def _scan(workspace_id: str, session: dict[str, Any], dispute: dict[str, Any]) -> Any:
    return scan_stripe_responses(
        payment_intents=(_payment_intent(workspace_id, payment_intent_id=PAYMENT_INTENT),),
        refunds=(),
        disputes=(dispute,),
        recorded_at=NOW,
        checkout_sessions={PAYMENT_INTENT: session},
        crediting_events={PAYMENT_INTENT: ("evt_checkout_1",)},
    )


@pytest.fixture(params=["spanner", "postgres"])
def reconciled_backend(
    request: pytest.FixtureRequest, nonreservation_stripe_payment: None
) -> Iterator[_Backend]:
    selected = _make_backend(request.param)
    configure_store(selected.store)
    try:
        yield selected
    finally:
        configure_store(InMemoryStore())


def test_closed_inquiry_reconciles_with_zero_semantic_mismatches(
    reconciled_backend: _Backend,
) -> None:
    workspace_id, session, client = _checkout_workspace(reconciled_backend)
    credited = reconciled_backend.balance(workspace_id)
    assert credited == 5_000_000
    opened = _inquiry_event(
        "evt_inquiry_opened", "warning_needs_response", days=2,
        event_type="charge.dispute.created",
    )
    closing = _inquiry_event(
        "evt_inquiry_closed", "warning_closed", days=122, event_type="charge.dispute.closed",
    )
    assert _post(client, opened)["outcome"] == "applied"
    assert _post(client, closing)["recovery_target"] == 0
    assert reconciled_backend.balance(workspace_id) == credited

    scan = _scan(workspace_id, session, _history_dispute("warning_closed", closing))
    assert [row.lifecycle_status for row in scan.adverse] == ["closed"]
    assert scan.outstanding == ()
    repository = trust_reconciliation_repository(reconciled_backend.store)
    diff = reconcile_canonical_mappings(
        canonical_mapping(canonical_records_from_events(scan.source_events)),
        canonical_mapping(canonical_records_from_events(repository.list_provider_events("stripe"))),
    )
    assert diff.semantic_mismatch_count == 0, diff
    assert diff.unmatched_count == 0, diff

    result = _backfill(repository, scan)
    assert result.marker.is_complete
    assert reconciled_backend.balance(workspace_id) == credited


def test_stored_closed_claim_is_reported_by_the_backfill_and_not_recredited(
    reconciled_backend: _Backend,
) -> None:
    """The previous mapping's debit for a closed inquiry surfaces to an operator.

    The history converter derives no claim for the closed inquiry while the
    stored row still holds one, so the plan and the backfill report a semantic
    mismatch. Applying the scan is a replay: no money moves.
    """

    workspace_id, session, client = _checkout_workspace(reconciled_backend)
    chargeback = _inquiry_event(
        "evt_legacy", "needs_response", days=2, event_type="charge.dispute.created",
    )
    assert _post(client, chargeback)["recovery_target"] == 5_000_000
    reconciled_backend.store_legacy_status(workspace_id, "dp_inquiry", "closed")
    closing = _inquiry_event(
        "evt_legacy_closed", "warning_closed", days=122, event_type="charge.dispute.closed",
    )
    # The closing Event restamps the stored row, as it did when the previous
    # mapping wrote it, so the only difference left is the claim itself.
    assert _post(client, closing)["outcome"] == "replay"
    scan = _scan(workspace_id, session, _history_dispute("warning_closed", closing))
    repository = trust_reconciliation_repository(reconciled_backend.store)

    plan = plan_historical_backfill(repository, scan, provider="stripe")
    (adverse_plan,) = plan.adverse
    assert (adverse_plan.action, adverse_plan.recovery_debit_micro) == ("replay", 0)
    diff = reconcile_canonical_mappings(
        canonical_mapping(canonical_records_from_events(scan.source_events)),
        canonical_mapping(canonical_records_from_events(repository.list_provider_events("stripe"))),
    )
    # Only the claim differs: the payment's derived target and the dispute's.
    assert diff.semantic_mismatches == (
        ("stripe", "dispute", "dp_inquiry"),
        ("stripe", "payment", PAYMENT_INTENT),
    ), diff

    result = _backfill(repository, scan)
    assert not result.marker.is_complete
    assert result.marker.semantic_mismatch_count == 2
    assert reconciled_backend.balance(workspace_id) == 0
    assert reconciled_backend.payment_recovery(workspace_id) == (5_000_000, 5_000_000, 0)
