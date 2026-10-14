"""The pilot's store methods (docs/design/fast-admission-pilot.md): a leased
authorize writes the authorization row and nothing else; a leased finalize
writes the terminal's records and no money."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditProvenance, Generation
from trusted_router.types import UsageType


def _store() -> tuple[Any, Any]:
    return make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
        analytics_outbox_enabled=True,
    )


def _funded_workspace(store: Any) -> tuple[Any, Any]:
    workspace = store.create_workspace(owner_user_id="owner", name="pilot")
    _raw, key = store.create_api_key(workspace_id=workspace.id, name="uncapped", creator_user_id="owner")
    assert store.credit_workspace_typed_direct(
        workspace.id, 5_000_000, "evt-pilot", provenance=CreditProvenance.system_grant(),
    ) is True
    return workspace, key


def _credit_rows(db: Any, workspace_id: str) -> list[dict[str, Any]]:
    return [
        dict(row) for (candidate, _shard), row in db.typed[CREDIT_BALANCE_TABLE].items() if candidate == workspace_id
    ]


def _reservations(db: Any, workspace_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in db.reservations.values() if row.get("workspace_id") == workspace_id]


def _activity(db: Any, workspace_id: str) -> list[dict[str, Any]]:
    rows = [json.loads(row["payload"]) for row in db.operational_analytics_outbox]
    return [row for row in rows if row.get("workspace_id") == workspace_id]


def _authorize(store: Any, workspace: Any, key: Any, *, envelope: str | None, authorization_id: str = "auth-leased-1") -> tuple[Any, Any]:
    return store.authorize_gateway_typed(
        workspace_id=workspace.id,
        key_hash=key.hash,
        authorization_id=authorization_id,
        estimate=1_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="model",
        provider="provider",
        requested_model_id=None,
        candidate_model_ids=["model"],
        region="us-central1",
        endpoint_id="endpoint",
        candidate_endpoint_ids=["endpoint"],
        idempotency_key=None,
        idempotency_fingerprint=None,
        skip_key_limit=True,
        fastpath_envelope=envelope,
        fastpath_end_of_life="2026-10-14T06:00:00Z",
    )


def test_a_leased_authorize_writes_the_authorization_row_and_holds_nothing() -> None:
    store, db = _store()
    workspace, key = _funded_workspace(store)
    before = _credit_rows(db, workspace.id)

    outcome, authorization = _authorize(store, workspace, key, envelope="sealed-1")

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert authorization.credit_reservation_id is None
    assert authorization.fastpath_envelope == "sealed-1"
    assert authorization.fastpath_end_of_life == "2026-10-14T06:00:00Z"
    assert _credit_rows(db, workspace.id) == before, "no credit hold"
    assert _reservations(db, workspace.id) == [], "no reservation row"
    stored = store.get_gateway_authorization(authorization.id)
    assert stored is not None
    assert stored.fastpath_envelope == "sealed-1" and stored.credit_reservation_id is None
    assert not stored.settled


def test_the_control_without_an_envelope_reserves_as_today() -> None:
    store, db = _store()
    workspace, key = _funded_workspace(store)
    outcome, authorization = _authorize(store, workspace, key, envelope=None)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None and authorization.credit_reservation_id is not None
    assert authorization.fastpath_envelope is None
    assert sum(row["reserved"] for row in _credit_rows(db, workspace.id)) == 1_000
    assert len(_reservations(db, workspace.id)) == 1


def test_a_leased_authorize_is_not_refused_by_the_unleased_headroom_check() -> None:
    """The synchronous path remembers a workspace it found out of credit and
    rechecks its unleased headroom first; a request the front door admitted
    was admitted from a lease's room, so that check does not apply."""
    store, db = _store()
    workspace, key = _funded_workspace(store)
    for (candidate, _shard), row in db.typed[CREDIT_BALANCE_TABLE].items():
        if candidate == workspace.id:
            row["reserved"] = row["total_credits"]
    store._insufficient_credit_workspaces.add(workspace.id)
    outcome, authorization = _authorize(store, workspace, key, envelope="sealed-1")
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None and authorization.fastpath_envelope == "sealed-1"


def test_a_leased_authorize_needs_its_id_up_front() -> None:
    store, _db = _store()
    workspace, key = _funded_workspace(store)
    with pytest.raises(ValueError):
        store.authorize_gateway_typed(
            workspace_id=workspace.id, key_hash=key.hash, estimate=1, has_credit_candidate=True,
            reservation_usage_type="Credits", model_id="m", provider="p", requested_model_id=None,
            candidate_model_ids=[], region=None, endpoint_id=None, candidate_endpoint_ids=[],
            idempotency_key=None, idempotency_fingerprint=None, fastpath_envelope="sealed",
        )


def _generation(authorization: Any, *, cost: int) -> Generation:
    return Generation(
        id="gen-1",
        request_id=authorization.id,
        workspace_id=authorization.workspace_id,
        key_hash=authorization.key_hash,
        model=authorization.model_id,
        provider_name="Provider",
        app="",
        tokens_prompt=10,
        tokens_completion=20,
        total_cost_microdollars=cost,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=40.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        provider=authorization.provider,
        region=authorization.region,
    )


def test_a_leased_settle_writes_the_records_and_books_no_money() -> None:
    store, db = _store()
    workspace, key = _funded_workspace(store)
    _outcome, authorization = _authorize(store, workspace, key, envelope="sealed-1")
    assert authorization is not None
    credit_before = _credit_rows(db, workspace.id)
    generation = _generation(authorization, cost=700)

    result = store.typed_finalize_leased_result(
        authorization.id, success=True, actual_microdollars=700, selected_usage_type="Credits",
        generation=generation, authorization_snapshot=authorization, mark_intent=None,
    )

    assert result is not None
    finalize, finalized = result
    assert finalize.finalized and finalize.activity_indexed and finalize.request_record_typed
    assert finalized.settled and finalized.finalization_outcome == "settled"
    assert finalized.finalized_cost_microdollars == 700
    stored = store.get_gateway_authorization(authorization.id)
    assert stored is not None and stored.settled and stored.finalized_generation_id == "gen-1"
    # Retention is the outbox's: the drain's done-mark arms it afterwards,
    # so this commit (the drain's shape, no intent of its own) leaves it unarmed.
    assert db.gateway_authorizations[authorization.id]["terminal_at"] is None
    assert stored.fastpath_envelope == "sealed-1"
    assert _credit_rows(db, workspace.id) == credit_before, "the lease booked the usage, not this commit"
    assert _reservations(db, workspace.id) == []
    activity = _activity(db, workspace.id)
    assert len(activity) == 1, activity


def test_a_leased_refund_marks_the_authorization_refunded_with_no_generation() -> None:
    store, db = _store()
    workspace, key = _funded_workspace(store)
    _outcome, authorization = _authorize(store, workspace, key, envelope="sealed-1")
    assert authorization is not None
    result = store.typed_finalize_leased_result(
        authorization.id, success=False, actual_microdollars=0, selected_usage_type="Credits",
        generation=None, authorization_snapshot=authorization, mark_intent=None,
    )
    assert result is not None
    _finalize, finalized = result
    assert finalized.settled and finalized.finalization_outcome == "refunded"
    assert _activity(db, workspace.id) == []


def test_a_second_leased_settle_is_a_replay_not_a_second_record() -> None:
    store, db = _store()
    workspace, key = _funded_workspace(store)
    _outcome, authorization = _authorize(store, workspace, key, envelope="sealed-1")
    assert authorization is not None
    generation = _generation(authorization, cost=700)
    first = store.typed_finalize_leased_result(
        authorization.id, success=True, actual_microdollars=700, selected_usage_type="Credits",
        generation=generation, authorization_snapshot=authorization, mark_intent=None,
    )
    assert first is not None
    # From the stored row this time: it is settled, so nothing is written.
    again = store.typed_finalize_leased_result(
        authorization.id, success=True, actual_microdollars=900, selected_usage_type="Credits",
        generation=generation, mark_intent=None,
    )
    assert again is None
    stored = store.get_gateway_authorization(authorization.id)
    assert stored is not None and stored.finalized_cost_microdollars == 700
    assert len(_activity(db, workspace.id)) == 1


def test_a_reserved_authorization_is_not_finalized_by_the_leased_path() -> None:
    store, _db = _store()
    workspace, key = _funded_workspace(store)
    _outcome, authorization = _authorize(store, workspace, key, envelope=None)
    assert authorization is not None
    assert store.typed_finalize_leased_result(
        authorization.id, success=True, actual_microdollars=1, selected_usage_type="Credits",
        generation=_generation(authorization, cost=1), authorization_snapshot=authorization,
    ) is None
    stored = store.get_gateway_authorization(authorization.id)
    assert stored is not None and not stored.settled
