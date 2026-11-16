"""Each backend latches on fraud evidence only and recovers principal as before.

Events are built by the live Stripe webhook builder from Stripe-shaped
payloads, so the refund reason and dispute stage travel the production path.
Every test checks the money (recovery target, recovered amount, balance)
before the latch, so a latch failure also shows the money was unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.routes.internal.webhook import _stripe_adverse_events
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
CREDITED = 1_000_000
#: A 60-cent refund of a 120-cent charge recovers half the credited principal.
REFUND_TARGET = 500_000


@dataclass
class Backend:
    store: Any
    workspace_id: str
    latched: Callable[[], list[bool]]
    balance: Callable[[], int]
    #: The fake Spanner database, for the inbox tests that rewrite a row.
    database: Any = None


def _credit(store: Any, workspace_id: str) -> None:
    assert store.credit_workspace_typed_direct(
        workspace_id,
        CREDITED,
        "evt_latch_payment",
        provenance=CreditProvenance("checkout", "stripe", "pi_latch", NOW),
        payment_amount_microdollars=1_200_000,
        currency="usd",
    )


def _spanner(*, funded: bool) -> Backend:
    store, database = make_fake_store()
    workspace_id = store.create_workspace("owner", "latch", trial_credit_microdollars=0).id
    if funded:
        _credit(store, workspace_id)

    def shards() -> list[dict[str, Any]]:
        return [row for (owner, _), row in database.typed["tr_credit_balance"].items() if owner == workspace_id]

    return Backend(
        store,
        workspace_id,
        lambda: [row["trust_latched_at"] is not None for row in shards()],
        lambda: sum(int(row["total_credits"]) for row in shards()),
        database,
    )


def _postgres(*, funded: bool) -> Backend:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    workspace_id = store.create_workspace("owner", "latch", trial_credit_microdollars=0).id
    if funded:
        _credit(store, workspace_id)

    def latched() -> list[bool]:
        rows = conn.execute(
            "SELECT trust_latched_at FROM tr_credit_balance WHERE workspace_id = %s", (workspace_id,)
        ).fetchall()
        return [row[0] is not None for row in rows]

    def balance() -> int:
        row = conn.execute(
            "SELECT SUM(total_credits) FROM tr_credit_balance WHERE workspace_id = %s", (workspace_id,)
        ).fetchone()
        return int(row[0])

    return Backend(store, workspace_id, latched, balance)


def _memory(*, funded: bool) -> Backend:
    store = InMemoryStore()
    workspace_id = store.create_workspace("owner", "latch", trial_credit_microdollars=0).id
    if funded:
        _credit(store, workspace_id)
    return Backend(
        store,
        workspace_id,
        lambda: [
            row["trust_latched_at"] is not None
            for (owner, _), row in store.credit_trust_shards.items()
            if owner == workspace_id
        ],
        lambda: int(store.credit_money_snapshot(workspace_id)[0]),
    )


BACKENDS: dict[str, Callable[..., Backend]] = {
    "spanner": _spanner,
    "postgres": _postgres,
    "memory": _memory,
}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request: pytest.FixtureRequest) -> Iterator[Backend]:
    yield BACKENDS[request.param](funded=True)


def _refund(
    *, status: str = "succeeded", reason: str | None = None, at: int = 1, event_type: str = "refund.updated",
) -> AdverseTrustEvent:
    refund = {
        "id": "re_latch", "object": "refund", "payment_intent": "pi_latch",
        "amount": 60, "status": status, "reason": reason,
    }
    obj: dict[str, Any] = refund
    if event_type == "charge.refunded":
        obj = {"id": "ch_latch", "object": "charge", "payment_intent": "pi_latch",
               "refunds": {"object": "list", "data": [refund]}}
    event = {
        "id": f"evt_refund_{at}", "object": "event", "type": event_type,
        "created": int(NOW.timestamp()) + at, "data": {"object": obj},
    }
    (adverse,) = _stripe_adverse_events(event, event_id=event["id"])
    return adverse


def _dispute(*, status: str, event_type: str, at: int) -> AdverseTrustEvent:
    event = {
        "id": f"evt_dispute_{at}", "object": "event", "type": event_type,
        "created": int(NOW.timestamp()) + at,
        "data": {"object": {
            "id": "dp_latch", "object": "dispute", "payment_intent": "pi_latch",
            "amount": 120, "status": status, "reason": "fraudulent",
        }},
    }
    (adverse,) = _stripe_adverse_events(event, event_id=event["id"])
    return adverse


def _money(backend: Backend, event: AdverseTrustEvent) -> tuple[str, int, int, int, int]:
    result = backend.store.record_adverse_trust_event(event)
    return (result.outcome, result.recovery_target, result.recovered_micro, result.unrecovered_micro, backend.balance())


@pytest.mark.parametrize("reason", [None, "requested_by_customer", "duplicate", "expired_uncaptured_charge"])
@pytest.mark.parametrize("event_type", ["refund.updated", "charge.refunded"])
def test_courtesy_refund_recovers_principal_without_latching(backend: Backend, reason: str | None, event_type: str) -> None:
    assert _money(backend, _refund(reason=reason, event_type=event_type)) == (
        "applied", REFUND_TARGET, REFUND_TARGET, 0, CREDITED - REFUND_TARGET,
    )
    latched = backend.latched()
    assert latched and not any(latched)


@pytest.mark.parametrize("event_type", ["refund.updated", "charge.refunded"])
def test_fraudulent_refund_recovers_principal_and_latches(backend: Backend, event_type: str) -> None:
    assert _money(backend, _refund(reason="fraudulent", event_type=event_type)) == (
        "applied", REFUND_TARGET, REFUND_TARGET, 0, CREDITED - REFUND_TARGET,
    )
    latched = backend.latched()
    assert latched and all(latched)


@pytest.mark.parametrize("status", ["pending", "failed", "canceled", "reversed"])
def test_fraudulent_refund_that_moves_no_money_still_latches(backend: Backend, status: str) -> None:
    # A refund to a stolen card often fails: the fraud call is the evidence,
    # whether or not the money moved. Nothing is recovered either way.
    assert _money(backend, _refund(status=status, reason="fraudulent")) == ("applied", 0, 0, 0, CREDITED)
    latched = backend.latched()
    assert latched and all(latched)


@pytest.mark.parametrize("status", ["pending", "failed", "canceled", "reversed"])
@pytest.mark.parametrize("reason", [None, "requested_by_customer"])
def test_courtesy_refund_that_moves_no_money_does_not_latch(backend: Backend, status: str, reason: str | None) -> None:
    assert _money(backend, _refund(status=status, reason=reason)) == ("applied", 0, 0, 0, CREDITED)
    latched = backend.latched()
    assert latched and not any(latched)


def test_pending_fraudulent_refund_latches_at_once_and_recovers_when_it_succeeds(backend: Backend) -> None:
    assert _money(backend, _refund(status="pending", reason="fraudulent", at=1)) == ("applied", 0, 0, 0, CREDITED)
    assert all(backend.latched())

    assert _money(backend, _refund(status="succeeded", reason="fraudulent", at=2)) == (
        "applied", REFUND_TARGET, REFUND_TARGET, 0, CREDITED - REFUND_TARGET,
    )
    latched = backend.latched()
    assert latched and all(latched)


def test_pending_fraudulent_refund_that_then_fails_stays_latched(backend: Backend) -> None:
    assert _money(backend, _refund(status="pending", reason="fraudulent", at=1)) == ("applied", 0, 0, 0, CREDITED)
    assert all(backend.latched())

    assert _money(backend, _refund(status="failed", reason="fraudulent", at=2)) == ("applied", 0, 0, 0, CREDITED)
    latched = backend.latched()
    assert latched and all(latched)


@pytest.mark.parametrize("status", ["warning_needs_response", "warning_under_review"])
def test_dispute_inquiry_claims_nothing_and_does_not_latch(backend: Backend, status: str) -> None:
    assert _money(backend, _dispute(status=status, event_type="charge.dispute.created", at=1)) == (
        "applied", 0, 0, 0, CREDITED,
    )
    latched = backend.latched()
    assert latched and not any(latched)


def test_inquiry_that_becomes_a_chargeback_latches_at_the_chargeback(backend: Backend) -> None:
    inquiry = _dispute(status="warning_needs_response", event_type="charge.dispute.created", at=1)
    assert _money(backend, inquiry) == ("applied", 0, 0, 0, CREDITED)
    assert not any(backend.latched())

    review = _dispute(status="warning_under_review", event_type="charge.dispute.updated", at=2)
    assert _money(backend, review) == ("replay", 0, 0, 0, CREDITED)
    assert not any(backend.latched())

    chargeback = _dispute(status="needs_response", event_type="charge.dispute.funds_withdrawn", at=3)
    assert _money(backend, chargeback) == ("applied", CREDITED, CREDITED, 0, 0)
    latched = backend.latched()
    assert latched and all(latched)


def test_chargeback_won_after_latching_stays_latched(backend: Backend) -> None:
    chargeback = _dispute(status="needs_response", event_type="charge.dispute.created", at=1)
    assert _money(backend, chargeback) == ("applied", CREDITED, CREDITED, 0, 0)
    assert all(backend.latched())

    won = _dispute(status="won", event_type="charge.dispute.closed", at=2)
    assert _money(backend, won) == ("applied", 0, 0, 0, CREDITED)
    latched = backend.latched()
    assert latched and all(latched)


def test_non_latching_event_after_a_fraud_latch_keeps_the_latch(backend: Backend) -> None:
    fraud = _refund(reason="fraudulent", at=1)
    assert _money(backend, fraud)[0] == "applied"
    assert all(backend.latched())

    # A later inquiry on the same payment does not latch, and nothing clears
    # the write-once latch already set.
    inquiry = _dispute(status="warning_needs_response", event_type="charge.dispute.created", at=2)
    assert _money(backend, inquiry) == (
        "applied", REFUND_TARGET, REFUND_TARGET, 0, CREDITED - REFUND_TARGET,
    )
    latched = backend.latched()
    assert latched and all(latched)


# ------------------------------------------------------------------ inbox
# Spanner and the in-memory store drain Stripe inbox rows when the payment
# arrives; the Postgres credit path drains only PayPal/Adyen rows.


def _strip_reason(payload: str) -> str:
    """The inbox payload as written before observations carried a reason."""

    body = json.loads(payload)
    body.pop("refund_reason", None)
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def _rewrite_inbox_as_legacy(backend: Backend) -> None:
    if isinstance(backend.store, InMemoryStore):
        ((key, row),) = backend.store.trust_inbox.items()
        backend.store.trust_inbox[key] = type(row)(row.provider, row.adverse_ref, _strip_reason(row.payload), row.received_at)
        return
    ((_key, row),) = backend.database.typed["tr_trust_inbox"].items()
    row["payload"] = _strip_reason(row["payload"])


@pytest.mark.parametrize("name", ["spanner", "memory"])
@pytest.mark.parametrize("reason,latches", [("fraudulent", True), ("requested_by_customer", False), (None, False)])
@pytest.mark.parametrize("legacy", [False, True], ids=["current", "legacy"])
def test_inbox_refund_drained_by_the_payment_latches_only_when_fraudulent(
    name: str, reason: str | None, latches: bool, legacy: bool,
) -> None:
    backend = BACKENDS[name](funded=False)
    assert backend.store.record_adverse_trust_event(_refund(reason=reason)).outcome == "inbox"
    if legacy:
        _rewrite_inbox_as_legacy(backend)

    _credit(backend.store, backend.workspace_id)

    assert backend.balance() == CREDITED - REFUND_TARGET
    latched = backend.latched()
    assert latched and all(value is latches for value in latched)
