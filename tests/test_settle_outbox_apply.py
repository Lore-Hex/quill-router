from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from google.api_core.exceptions import (
    Aborted,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    RetryError,
    ServiceUnavailable,
)

from tests.fakes.spanner import make_fake_store
from trusted_router.app_markup_billing import (
    APP_MARKUP_APP_ID_SETTLE_FIELD,
    APP_MARKUP_OWNER_SETTLE_FIELD,
    APP_MARKUP_PAYOUT_SETTLE_FIELD,
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
    app_markup_payout_event_id,
)
from trusted_router.catalog_data import PARASAIL_LIBERTY_2_0_MODEL_ID
from trusted_router.custom_model_billing import (
    USER_MODEL_ID_SETTLE_FIELD,
    USER_MODEL_OWNER_SETTLE_FIELD,
    USER_MODEL_PAYOUT_SETTLE_FIELD,
    user_model_payout_event_id,
)
from trusted_router.custom_model_markup_billing import (
    CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD,
    CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD,
    CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD,
    CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD,
    collected_custom_model_markup_microdollars,
    custom_model_markup_microdollars,
    custom_model_markup_owner_share_microdollars,
    custom_model_markup_payout_event_id,
)
from trusted_router.partner_billing import PARTNER_OPERATOR_COST_SETTLE_FIELD
from trusted_router.services import settle_outbox_apply as apply_mod
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome, SettleOutcome, settle_atomic
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
)
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import CreditAccount, GatewayAuthorization, SettleOutboxRow

MODEL_ID = "anthropic/claude-haiku-4.5"
PROVIDER = "anthropic"
ENDPOINT_ID = "anthropic/claude-haiku-4.5@anthropic/prepaid"
ESTIMATE = 1_000_000
TOTAL_CREDIT = 5_000_000
USER_MODEL_ID = "tr-user-model/test-outbox-payout"
USER_MODEL_ENDPOINT_ID = f"{USER_MODEL_ID}@trustedrouter/credits"
CUSTOM_MARKUP_MODEL_ID = "tr-custom-model/test-outbox-markup"


@pytest.fixture
def fake_store() -> Iterator[tuple[Any, Any, Any]]:
    store, db, bt = make_fake_store()
    configure_store(store)
    try:
        yield store, db, bt
    finally:
        configure_store(InMemoryStore())


def _seed_credit(store: Any, workspace_id: str, total: int = TOTAL_CREDIT) -> None:
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id),
    )
    store._database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace_id, 0)] = {
        "workspace_id": workspace_id,
        "shard": 0,
        "total_credits": total,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }


def _make_key(store: Any, workspace_id: str, *, limit: int | None = TOTAL_CREDIT) -> Any:
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="primary",
        creator_user_id=None,
        limit_microdollars=limit,
    )
    return key


def _typed_credit(db: Any, workspace_id: str) -> dict[str, Any]:
    return db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]


def _typed_key(db: Any, key_hash: str) -> dict[str, Any]:
    return db.typed[KEY_LIMIT_TABLE][(key_hash, 0)]


def _typed_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int = ESTIMATE,
) -> GatewayAuthorization:
    outcome, auth = store.authorize_gateway_typed(
        workspace_id=workspace_id,
        key_hash=key_hash,
        estimate=estimate,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id=MODEL_ID,
        provider=PROVIDER,
        requested_model_id=MODEL_ID,
        candidate_model_ids=[MODEL_ID],
        region="us",
        endpoint_id=ENDPOINT_ID,
        candidate_endpoint_ids=[ENDPOINT_ID],
        idempotency_key=None,
        idempotency_fingerprint=None,
        expires_at="2026-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert auth is not None
    return auth


def _typed_app_authorization(
    store: Any, *, workspace_id: str, key_hash: str, markup_basis_points: int = 500
) -> GatewayAuthorization:
    outcome, auth = store.authorize_gateway_typed(
        workspace_id=workspace_id,
        key_hash=key_hash,
        estimate=ESTIMATE,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id=MODEL_ID,
        provider=PROVIDER,
        requested_model_id=MODEL_ID,
        candidate_model_ids=[MODEL_ID],
        region="us",
        endpoint_id=ENDPOINT_ID,
        candidate_endpoint_ids=[ENDPOINT_ID],
        idempotency_key=None,
        idempotency_fingerprint=None,
        app_id="app-markup",
        app_markup_basis_points=markup_basis_points,
        app_owner_user_id="owner-app-markup",
        expires_at="2026-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert auth is not None
    return auth


def _typed_custom_markup_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    markup_basis_points: int = 1_500,
) -> GatewayAuthorization:
    outcome, auth = store.authorize_gateway_typed(
        workspace_id=workspace_id,
        key_hash=key_hash,
        estimate=ESTIMATE,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id=MODEL_ID,
        provider=PROVIDER,
        requested_model_id=CUSTOM_MARKUP_MODEL_ID,
        candidate_model_ids=[MODEL_ID],
        region="us",
        endpoint_id=ENDPOINT_ID,
        candidate_endpoint_ids=[ENDPOINT_ID],
        idempotency_key=None,
        idempotency_fingerprint=None,
        custom_model_id=CUSTOM_MARKUP_MODEL_ID,
        custom_model_revision=4,
        custom_model_markup_basis_points=markup_basis_points,
        custom_model_owner_user_id="owner-custom-markup",
        expires_at="2026-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert auth is not None
    return auth


def _legacy_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int = ESTIMATE,
    app_id: str = "",
    app_markup_basis_points: int = 0,
    app_owner_user_id: str = "",
) -> GatewayAuthorization:
    reservation_id = f"legacy-res-{workspace_id}"
    credit = store._read_entity("credit", workspace_id, dict)
    if credit is not None:
        credit["reserved_microdollars"] = int(credit.get("reserved_microdollars", 0)) + estimate
        store._write_entity("credit", workspace_id, credit)
    store.reserve_key_limit(key_hash, estimate, usage_type="Credits")
    return store.create_gateway_authorization(
        workspace_id=workspace_id,
        key_hash=key_hash,
        model_id=MODEL_ID,
        provider=PROVIDER,
        usage_type="Credits",
        estimated_microdollars=estimate,
        credit_reservation_id=reservation_id,
        requested_model_id=MODEL_ID,
        candidate_model_ids=[MODEL_ID],
        region="us",
        endpoint_id=ENDPOINT_ID,
        candidate_endpoint_ids=[ENDPOINT_ID],
        app_id=app_id,
        app_markup_basis_points=app_markup_basis_points,
        app_owner_user_id=app_owner_user_id,
    )


def _typed_user_model_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
) -> GatewayAuthorization:
    outcome, auth = store.authorize_gateway_typed(
        workspace_id=workspace_id,
        key_hash=key_hash,
        estimate=ESTIMATE,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id=USER_MODEL_ID,
        provider="trustedrouter",
        requested_model_id=USER_MODEL_ID,
        candidate_model_ids=[USER_MODEL_ID],
        region="us",
        endpoint_id=USER_MODEL_ENDPOINT_ID,
        candidate_endpoint_ids=[USER_MODEL_ENDPOINT_ID],
        idempotency_key="typed-user-model-payout",
        idempotency_fingerprint="typed-user-model-payout-fingerprint",
        user_provided_model_id=USER_MODEL_ID,
        user_provided_model_revision=3,
        user_model_prompt_price_microdollars_per_m=2_000_000,
        user_model_completion_price_microdollars_per_m=3_000_000,
        user_model_owner_user_id="owner-user-model-payout",
        expires_at="2026-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert auth is not None
    return auth


def _settle_body(authorization_id: str, *, endpoint_id: str = ENDPOINT_ID) -> str:
    return json.dumps(
        {
            "authorization_id": authorization_id,
            "actual_input_tokens": 14,
            "actual_output_tokens": 7,
            "cache_read_input_tokens": 6_081,
            "cache_creation_input_tokens": 2,
            "request_id": f"req-{authorization_id}",
            "finish_reason": "stop",
            "status": "success",
            "streamed": True,
            "elapsed_seconds": 2.0,
            "selected_model": MODEL_ID,
            "selected_endpoint": endpoint_id,
        }
    )


def _row(
    auth: GatewayAuthorization,
    *,
    origin: str = "typed",
    intent: str = "settle",
    cost: int = 777_777,
    endpoint_id: str = ENDPOINT_ID,
    model_id: str = MODEL_ID,
    settle_body: str | None = None,
) -> SettleOutboxRow:
    return SettleOutboxRow(
        authorization_id=auth.id,
        intent_kind=intent,
        settle_origin=origin,
        actual_cost_micro=cost,
        reservation_id=auth.credit_reservation_id,
        selected_endpoint_id=endpoint_id,
        model_id=model_id,
        selected_usage_type="Credits",
        settle_body=settle_body if settle_body is not None else _settle_body(auth.id, endpoint_id=endpoint_id),
    )


def _generation_bodies(db: Any) -> list[dict[str, Any]]:
    legacy = [
        json.loads(row.body)
        for (kind, _entity_id), row in db.rows.items()
        if kind == "generation"
    ]
    typed = [json.loads(record["payload"]) for record in db.generation_records.values()]
    return legacy + typed


def _stamp_spend_lease_binding(
    db: Any,
    auth: GatewayAuthorization,
    *,
    allocation_micro: int,
) -> None:
    record = db.gateway_authorizations[auth.id]
    payload = json.loads(record["payload"])
    binding = {
        "settlement": "spend_lease",
        "spend_lease_id": "lease-repair",
        "spend_lease_gen": 8,
        "spend_lease_allocated_micro": allocation_micro,
    }
    payload.update(binding)
    record.update(binding)
    record["payload"] = json.dumps(payload, separators=(",", ":"))


def _enable_typed_generation_durability(store: Any) -> None:
    outbox = SpannerOperationalAnalyticsOutbox(store._database, store._param_types)
    store._generation_records_enabled = True
    store._operational_analytics_outbox = outbox
    store.generation_store._generation_records_enabled = True
    store.generation_store._operational_analytics_outbox = outbox


class _TypedStoreProxy:
    def __init__(
        self,
        store: Any,
        *,
        finalize_result: dict[str, Any] | None = None,
        finalize_exc: Exception | None = None,
        read_exc: Exception | None = None,
    ) -> None:
        self._store = store
        self._finalize_result = finalize_result
        self._finalize_exc = finalize_exc
        self._read_exc = read_exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def typed_finalize_gateway(self, **_kwargs: Any) -> dict[str, Any]:
        if self._finalize_exc is not None:
            raise self._finalize_exc
        if self._finalize_result is not None:
            return self._finalize_result
        return self._store.typed_finalize_gateway(**_kwargs)

    def read_typed_reservation(self, reservation_id: str) -> dict[str, Any] | None:
        if self._read_exc is not None:
            raise self._read_exc
        return self._store.read_typed_reservation(reservation_id)


def test_typed_settle_applies_frozen_cost(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_typed"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    assert apply_frozen_settle(_row(auth, cost=777_777)) == ApplyOutcome.SETTLED_NOW

    assert _typed_credit(db, ws)["total_usage"] == 777_777
    reservation = db.reservations[auth.credit_reservation_id]
    assert reservation["settled"] is True
    assert reservation["actual_micro"] == 777_777
    generations = _generation_bodies(db)
    assert len(generations) == 1
    assert generations[0]["total_cost_microdollars"] == 777_777
    assert generations[0]["tokens_prompt"] == 6_097
    assert store.get_gateway_authorization(auth.id).settled is True


def test_partner_replay_preserves_public_model_and_provider(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_partner"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    row = _row(
        auth,
        cost=13_808,
        model_id=PARASAIL_LIBERTY_2_0_MODEL_ID,
    )
    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW

    generations = _generation_bodies(db)
    assert len(generations) == 1
    assert generations[0]["model"] == PARASAIL_LIBERTY_2_0_MODEL_ID
    assert generations[0]["provider"] == "parasail"
    assert generations[0]["total_cost_microdollars"] == 13_808


def test_partner_replay_preserves_internal_operator_cost(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_partner_operator_cost"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    body = json.loads(_settle_body(auth.id))
    body[PARTNER_OPERATOR_COST_SETTLE_FIELD] = 654_321

    row = _row(auth, cost=0, settle_body=json.dumps(body))
    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW

    generation = _generation_bodies(db)[0]
    assert generation["total_cost_microdollars"] == 0
    assert generation["operator_cost_microdollars"] == 654_321


def test_replay_reports_already_charged(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_replay"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, cost=777_777)

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert _typed_credit(db, ws)["total_usage"] == 777_777
    assert len(_generation_bodies(db)) == 1


def test_late_settle_after_reaped_snapshot_preserves_money_and_heartbeat_generation(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    ws = "ws-late-after-reaped-snapshot"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    snapshot_cost = 120
    reservation = db.reservations[auth.credit_reservation_id]
    credit = _typed_credit(db, ws)
    key_row = _typed_key(db, key.hash)
    reservation.update(settled=True, actual_micro=snapshot_cost)
    credit.update(
        reserved=credit["reserved"] - ESTIMATE,
        total_usage=snapshot_cost,
    )
    key_row.update(
        reserved=key_row["reserved"] - ESTIMATE,
        usage=snapshot_cost,
    )
    record = db.gateway_authorizations[auth.id]
    payload = json.loads(record["payload"])
    payload.update(
        settled=True,
        finalization_outcome="reaped_snapshot",
        finalized_cost_microdollars=snapshot_cost,
        finalized_generation_id="heartbeat-generation",
    )
    record.update(
        settled=True,
        finalization_outcome="reaped_snapshot",
        finalized_cost_microdollars=snapshot_cost,
        payload=json.dumps(payload, separators=(",", ":")),
    )
    heartbeat_payload = json.dumps(
        {
            "id": "heartbeat-generation",
            "settled_from": "heartbeat",
            "usage_estimated": True,
        },
        separators=(",", ":"),
    )
    db.generation_records["heartbeat-generation"] = {
        "generation_id": "heartbeat-generation",
        "payload": heartbeat_payload,
    }
    late = _row(auth, cost=777_777)

    outcome = apply_frozen_settle(late)

    assert outcome == ApplyOutcome.REAPED_SNAPSHOT
    assert reservation["actual_micro"] == snapshot_cost
    assert credit["total_usage"] == snapshot_cost
    assert key_row["usage"] == snapshot_cost
    assert late.actual_cost_micro == 777_777
    assert db.generation_records == {
        "heartbeat-generation": {
            "generation_id": "heartbeat-generation",
            "payload": heartbeat_payload,
        }
    }


def test_user_model_outbox_repair_pays_owner_exactly_once(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_user_model_payout"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    payout = 5_600
    body = {
        "authorization_id": auth.id,
        "actual_input_tokens": 1_000,
        "actual_output_tokens": 2_000,
        "request_id": f"req-{auth.id}",
        "finish_reason": "stop",
        "status": "success",
        "elapsed_seconds": 0.2,
        "selected_model": USER_MODEL_ID,
        "selected_endpoint": USER_MODEL_ENDPOINT_ID,
        USER_MODEL_PAYOUT_SETTLE_FIELD: payout,
        USER_MODEL_OWNER_SETTLE_FIELD: "owner-user-model-payout",
        USER_MODEL_ID_SETTLE_FIELD: USER_MODEL_ID,
        PARTNER_OPERATOR_COST_SETTLE_FIELD: payout,
    }
    row = _row(
        auth,
        cost=8_000,
        endpoint_id=USER_MODEL_ENDPOINT_ID,
        model_id=USER_MODEL_ID,
        settle_body=json.dumps(body),
    )

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert store.earnings_summary("owner-user-model-payout") == {
        "total_earned": payout,
        "total_transferred": 0,
        "available": payout,
    }
    movements = store.list_credit_movements("user:owner-user-model-payout")
    assert len(movements) == 1
    assert movements[0].movement_id == user_model_payout_event_id(auth.id)
    assert movements[0].amount_microdollars == payout
    assert movements[0].counterparty_account_id == ws
    assert movements[0].custom_model_id == USER_MODEL_ID
    assert movements[0].authorization_id == auth.id
    generations = _generation_bodies(db)
    assert len(generations) == 1
    assert generations[0]["custom_model_id"] == USER_MODEL_ID
    assert generations[0]["operator_cost_microdollars"] == payout


def test_typed_app_markup_settle_and_outbox_replay_book_exact_money_once(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_app_markup"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(store, workspace_id=ws, key_hash=key.hash)
    markup = app_markup_microdollars_from_charge(800_000, 500)
    payout = app_markup_owner_share_microdollars(markup)
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            APP_MARKUP_PAYOUT_SETTLE_FIELD: payout,
            APP_MARKUP_OWNER_SETTLE_FIELD: "owner-app-markup",
            APP_MARKUP_APP_ID_SETTLE_FIELD: "app-markup",
        }
    )
    row = _row(auth, cost=800_000, settle_body=json.dumps(body))

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert _typed_credit(db, ws)["total_usage"] == 800_000
    assert store.earnings_summary("owner-app-markup")["total_earned"] == payout
    movements = store.list_credit_movements("user:owner-app-markup")
    assert len(movements) == 1
    assert movements[0].movement_id == app_markup_payout_event_id(auth.id)
    assert movements[0].kind == "app_markup_payout"
    assert movements[0].amount_microdollars == payout
    assert markup - payout == 11_429


def test_typed_custom_markup_settle_and_replay_book_exact_money_once(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    workspace_id = "ws_apply_custom_markup"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    auth = _typed_custom_markup_authorization(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    token_cost = 800_000
    markup = custom_model_markup_microdollars(
        token_cost,
        auth.custom_model_markup_basis_points,
    )
    charge = token_cost + markup
    payout = custom_model_markup_owner_share_microdollars(markup)
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD: markup,
            CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD: payout,
            CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD: auth.custom_model_owner_user_id,
            CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD: auth.custom_model_id,
        }
    )
    row = _row(auth, cost=charge, settle_body=json.dumps(body))

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert _typed_credit(db, workspace_id)["total_usage"] == charge
    assert store.earnings_summary(auth.custom_model_owner_user_id)["total_earned"] == payout
    movements = store.list_credit_movements(
        f"user:{auth.custom_model_owner_user_id}"
    )
    assert len(movements) == 1
    assert movements[0].movement_id == custom_model_markup_payout_event_id(auth.id)
    assert movements[0].kind == "custom_model_markup_payout"
    assert movements[0].custom_model_id == CUSTOM_MARKUP_MODEL_ID
    assert movements[0].authorization_id == auth.id
    generations = _generation_bodies(db)
    assert len(generations) == 1
    assert generations[0]["custom_model_id"] == CUSTOM_MARKUP_MODEL_ID
    assert generations[0]["custom_model_markup_microdollars"] == markup


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        (CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD, 1),
        (CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD, 1),
        (CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD, "forged-owner"),
        (CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD, "tr-custom-model/forged-model"),
    ],
)
def test_forged_custom_markup_outbox_fields_do_not_charge_or_pay(
    fake_store: tuple[Any, Any, Any],
    field: str,
    forged: Any,
) -> None:
    store, db, _bt = fake_store
    workspace_id = f"ws_forged_custom_markup_{field[-12:]}"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    auth = _typed_custom_markup_authorization(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    token_cost = 800_000
    markup = custom_model_markup_microdollars(
        token_cost,
        auth.custom_model_markup_basis_points,
    )
    payout = custom_model_markup_owner_share_microdollars(markup)
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD: markup,
            CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD: payout,
            CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD: auth.custom_model_owner_user_id,
            CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD: auth.custom_model_id,
            field: forged,
        }
    )

    assert (
        apply_frozen_settle(
            _row(auth, cost=token_cost + markup, settle_body=json.dumps(body))
        )
        == ApplyOutcome.INVALID_ROW
    )
    assert _typed_credit(db, workspace_id)["total_usage"] == 0
    assert store.earnings_summary(auth.custom_model_owner_user_id)["total_earned"] == 0
    assert store.get_gateway_authorization(auth.id).settled is False


def test_missing_custom_markup_outbox_fields_fail_closed(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    workspace_id = "ws_missing_custom_markup_fields"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    auth = _typed_custom_markup_authorization(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )

    assert apply_frozen_settle(_row(auth, cost=900_000)) == ApplyOutcome.INVALID_ROW
    assert _typed_credit(db, workspace_id)["total_usage"] == 0
    assert store.earnings_summary(auth.custom_model_owner_user_id)["total_earned"] == 0


def test_regional_clamp_frozen_payout_replays_without_dead_letter(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_regional_clamp_markup"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(
        store, workspace_id=ws, key_hash=key.hash, markup_basis_points=30_000
    )
    charge = 400
    markup = app_markup_microdollars_from_charge(charge, auth.app_markup_basis_points)
    payout = app_markup_owner_share_microdollars(markup)
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            APP_MARKUP_PAYOUT_SETTLE_FIELD: payout,
            APP_MARKUP_OWNER_SETTLE_FIELD: auth.app_owner_user_id,
            APP_MARKUP_APP_ID_SETTLE_FIELD: auth.app_id,
        }
    )
    row = _row(auth, cost=charge, settle_body=json.dumps(body))

    assert body[APP_MARKUP_PAYOUT_SETTLE_FIELD] == 210
    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert _typed_credit(db, ws)["total_usage"] == charge
    movements = store.list_credit_movements(f"user:{auth.app_owner_user_id}")
    assert len(movements) == 1
    assert movements[0].amount_microdollars == payout


def test_missing_frozen_app_markup_fields_derive_payout_from_authorization(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_app_markup_missing_fields"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(store, workspace_id=ws, key_hash=key.hash)
    charge = 800_000
    payout = app_markup_owner_share_microdollars(
        app_markup_microdollars_from_charge(charge, auth.app_markup_basis_points)
    )

    assert apply_frozen_settle(_row(auth, cost=charge)) == ApplyOutcome.SETTLED_NOW
    assert _typed_credit(db, ws)["total_usage"] == charge
    assert store.earnings_summary(auth.app_owner_user_id)["total_earned"] == payout


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        ("owner-app-markup", ApplyOutcome.SETTLED_NOW),
        ("forged-owner", ApplyOutcome.INVALID_ROW),
    ],
)
def test_partial_frozen_app_markup_owner_is_only_a_cross_check(
    fake_store: tuple[Any, Any, Any], owner: str, expected: str
) -> None:
    store, db, _bt = fake_store
    ws = f"ws_apply_app_markup_partial_{owner}"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(store, workspace_id=ws, key_hash=key.hash)
    charge = 800_000
    payout = app_markup_owner_share_microdollars(
        app_markup_microdollars_from_charge(charge, auth.app_markup_basis_points)
    )
    body = json.loads(_settle_body(auth.id))
    body[APP_MARKUP_OWNER_SETTLE_FIELD] = owner

    assert (
        apply_frozen_settle(_row(auth, cost=charge, settle_body=json.dumps(body)))
        == expected
    )
    expected_usage = charge if expected == ApplyOutcome.SETTLED_NOW else 0
    expected_payout = payout if expected == ApplyOutcome.SETTLED_NOW else 0
    assert _typed_credit(db, ws)["total_usage"] == expected_usage
    assert store.earnings_summary(auth.app_owner_user_id)["total_earned"] == expected_payout


def test_zero_markup_authorization_rejects_frozen_payout_fields(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_zero_markup_forged_fields"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    body = json.loads(_settle_body(auth.id))
    body[APP_MARKUP_OWNER_SETTLE_FIELD] = "forged-owner"

    assert (
        apply_frozen_settle(_row(auth, cost=800_000, settle_body=json.dumps(body)))
        == ApplyOutcome.INVALID_ROW
    )
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert store.earnings_summary("forged-owner")["total_earned"] == 0


def test_zero_markup_authorization_without_payout_fields_charges_only(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_zero_markup_no_fields"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    assert apply_frozen_settle(_row(auth, cost=800_000)) == ApplyOutcome.SETTLED_NOW
    assert _typed_credit(db, ws)["total_usage"] == 800_000
    assert store.list_credit_movements("user:") == []


def test_unclaimed_frozen_overcharge_is_corrected_atomically_and_crash_replays_identically(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    _enable_typed_generation_durability(store)
    ws = "ws-spend-lease-corrective-repair"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(
        store,
        workspace_id=ws,
        key_hash=key.hash,
        markup_basis_points=2_500,
    )
    allocation = 400_000
    _stamp_spend_lease_binding(db, auth, allocation_micro=allocation)
    old_markup = app_markup_microdollars_from_charge(800_000, auth.app_markup_basis_points)
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            APP_MARKUP_PAYOUT_SETTLE_FIELD: app_markup_owner_share_microdollars(old_markup),
            APP_MARKUP_OWNER_SETTLE_FIELD: auth.app_owner_user_id,
            APP_MARKUP_APP_ID_SETTLE_FIELD: auth.app_id,
        }
    )
    outbox = SpannerSettleOutbox(db, store._param_types)
    outbox.enqueue(_row(auth, cost=800_000, settle_body=json.dumps(body)))
    [claimed] = outbox.claim(limit=1)

    with caplog.at_level(logging.ERROR):
        assert apply_frozen_settle(claimed) == ApplyOutcome.SETTLED_NOW

    repaired = db.settle_outbox[(auth.id, "settle")]
    repaired_body = json.loads(repaired["settle_body"])
    repaired_markup = app_markup_microdollars_from_charge(
        allocation, auth.app_markup_basis_points
    )
    repaired_payout = app_markup_owner_share_microdollars(repaired_markup)
    assert repaired["actual_cost_micro"] == allocation
    assert repaired_body[APP_MARKUP_PAYOUT_SETTLE_FIELD] == repaired_payout
    assert _typed_credit(db, ws)["total_usage"] == allocation
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == allocation
    assert db.gateway_authorizations[auth.id]["finalized_cost_microdollars"] == allocation
    assert db.gateway_authorizations[auth.id]["terminal_at"] is None
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is None
    [generation] = _generation_bodies(db)
    assert generation["total_cost_microdollars"] == allocation
    assert generation["app_markup_microdollars"] == repaired_markup
    [analytics_intent] = db.operational_analytics_outbox
    assert json.loads(analytics_intent["payload"])["total_cost_microdollars"] == allocation
    assert store.earnings_summary(auth.app_owner_user_id)["total_earned"] == repaired_payout
    assert "spend_lease.frozen_charge_capped_at_allocation" in caplog.text

    # Crash before mark(done): the corrected row remains the sole replay authority.
    repaired["leased_until"] = "2000-01-01T00:00:00Z"
    [reclaimed] = outbox.claim(limit=1)
    assert reclaimed.actual_cost_micro == allocation
    assert apply_frozen_settle(reclaimed) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert _typed_credit(db, ws)["total_usage"] == allocation
    assert store.earnings_summary(auth.app_owner_user_id)["total_earned"] == repaired_payout
    assert outbox.mark(
        auth.id,
        "settle",
        done=True,
        lease_owner=reclaimed.lease_owner,
    ) == "done"
    assert db.gateway_authorizations[auth.id]["terminal_at"] is not None
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is not None


def test_spend_lease_repair_pays_only_collected_custom_model_markup(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    _enable_typed_generation_durability(store)
    ws = "ws-spend-lease-custom-markup-repair"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_custom_markup_authorization(
        store,
        workspace_id=ws,
        key_hash=key.hash,
    )
    allocation = 400_000
    original_charge = 900_000
    _stamp_spend_lease_binding(db, auth, allocation_micro=allocation)
    original_markup = collected_custom_model_markup_microdollars(
        original_charge,
        auth.custom_model_markup_basis_points,
    )
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD: original_markup,
            CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD: (
                custom_model_markup_owner_share_microdollars(original_markup)
            ),
            CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD: auth.custom_model_owner_user_id,
            CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD: auth.custom_model_id,
        }
    )
    outbox = SpannerSettleOutbox(db, store._param_types)
    outbox.enqueue(
        _row(auth, cost=original_charge, settle_body=json.dumps(body))
    )
    [claimed] = outbox.claim(limit=1)

    assert apply_frozen_settle(claimed) == ApplyOutcome.SETTLED_NOW

    collected_markup = collected_custom_model_markup_microdollars(
        allocation,
        auth.custom_model_markup_basis_points,
    )
    payout = custom_model_markup_owner_share_microdollars(collected_markup)
    repaired = db.settle_outbox[(auth.id, "settle")]
    repaired_body = json.loads(repaired["settle_body"])
    assert repaired["actual_cost_micro"] == allocation
    assert (
        repaired_body[CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD]
        == collected_markup
    )
    assert repaired_body[CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD] == payout
    assert _typed_credit(db, ws)["total_usage"] == allocation
    assert store.earnings_summary(auth.custom_model_owner_user_id)["total_earned"] == payout
    [generation] = _generation_bodies(db)
    assert generation["total_cost_microdollars"] == allocation
    assert generation["custom_model_markup_microdollars"] == collected_markup


def test_ownerless_corrective_settle_returns_error_without_rewrite_claim_or_charge(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    ws = "ws-spend-lease-ownerless-corrective"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    allocation = 400_000
    frozen_cost = 800_000
    _stamp_spend_lease_binding(db, auth, allocation_micro=allocation)
    row = _row(auth, cost=frozen_cost)
    original_body = row.settle_body
    assert row.lease_owner is None

    assert apply_frozen_settle(row) == ApplyOutcome.ERROR

    assert row.actual_cost_micro == frozen_cost
    assert row.settle_body == original_body
    assert db.settle_outbox == {}
    assert db.reservations[auth.credit_reservation_id]["settled"] is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _generation_bodies(db) == []


def test_lost_outbox_lease_rolls_back_corrective_finalization(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    _enable_typed_generation_durability(store)
    ws = "ws-spend-lease-lost-repair-fence"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    _stamp_spend_lease_binding(db, auth, allocation_micro=400_000)
    outbox = SpannerSettleOutbox(db, store._param_types)
    outbox.enqueue(_row(auth, cost=800_000))
    [claimed] = outbox.claim(limit=1)
    db.settle_outbox[(auth.id, "settle")]["lease_owner"] = "newer-worker"

    assert apply_frozen_settle(claimed) == ApplyOutcome.ERROR

    assert db.reservations[auth.credit_reservation_id]["settled"] is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert db.settle_outbox[(auth.id, "settle")]["actual_cost_micro"] == 800_000
    assert _generation_bodies(db) == []


def test_already_finalized_historical_overcharge_is_unchanged_logged_and_replayed(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    _enable_typed_generation_durability(store)
    ws = "ws-spend-lease-historical-overcharge"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    allocation = 400_000
    historical = 800_000
    _stamp_spend_lease_binding(db, auth, allocation_micro=allocation)
    reservation = db.reservations[auth.credit_reservation_id]
    reservation.update(settled=True, actual_micro=historical)
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update(
        total_usage=historical,
        reserved=0,
    )
    record = db.gateway_authorizations[auth.id]
    record.update(
        settled=True,
        finalization_outcome="settled",
        finalized_cost_microdollars=historical,
    )
    payload = json.loads(record["payload"])
    payload.update(
        settled=True,
        finalization_outcome="settled",
        finalized_cost_microdollars=historical,
    )
    record["payload"] = json.dumps(payload)
    outbox = SpannerSettleOutbox(db, store._param_types)
    outbox.enqueue(_row(auth, cost=900_000))
    [claimed] = outbox.claim(limit=1)

    with caplog.at_level(logging.ERROR):
        outcome = apply_frozen_settle(claimed)

    assert outcome == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert reservation["actual_micro"] == historical
    assert _typed_credit(db, ws)["total_usage"] == historical
    [generation] = _generation_bodies(db)
    assert generation["total_cost_microdollars"] == historical
    [event] = [
        record
        for record in caplog.records
        if record.getMessage() == "spend_lease.historical_overcharge"
    ]
    event_fields = vars(event)
    assert event_fields["finalized_cost_microdollars"] == historical
    assert event_fields["spend_lease_allocated_micro"] == allocation
    assert event_fields["authorization_id"] == auth.id
    assert event_fields["spend_lease_id"] == "lease-repair"


def test_two_spend_lease_repairs_book_at_most_once(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    ws = "ws-spend-lease-two-repairs"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    allocation = 400_000
    _stamp_spend_lease_binding(db, auth, allocation_micro=allocation)
    outbox = SpannerSettleOutbox(db, store._param_types)
    outbox.enqueue(_row(auth, cost=800_000))
    [claimed] = outbox.claim(limit=1)
    stale_worker_view = replace(claimed)

    assert apply_frozen_settle(claimed) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(stale_worker_view) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE

    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == allocation
    assert _typed_credit(db, ws)["total_usage"] == allocation


def test_spend_lease_repair_losing_to_reaper_never_books_charge(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    ws = "ws-spend-lease-repair-vs-reaper"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    _stamp_spend_lease_binding(db, auth, allocation_micro=400_000)
    outbox = SpannerSettleOutbox(db, store._param_types)
    outbox.enqueue(_row(auth, cost=800_000))
    [claimed] = outbox.claim(limit=1)
    freed = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=auth.credit_reservation_id,
        actual_micro=0,
        settled_usage_type="Credits",
        success=False,
        guard_outbox=False,
    )
    assert freed["outcome"] == SettleOutcome.SETTLED

    assert apply_frozen_settle(claimed) == ApplyOutcome.ALREADY_RELEASED_FREE
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == 0
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _generation_bodies(db) == []


def test_missing_frozen_app_markup_fields_replay_credits_once(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, _db, _bt = fake_store
    ws = "ws_apply_app_markup_missing_fields_replay"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, cost=800_000)

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    movements = store.list_credit_movements(f"user:{auth.app_owner_user_id}")
    assert [movement.movement_id for movement in movements] == [
        app_markup_payout_event_id(auth.id)
    ]


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        (APP_MARKUP_PAYOUT_SETTLE_FIELD, 999_999),
        (APP_MARKUP_OWNER_SETTLE_FIELD, "forged-owner"),
        (APP_MARKUP_APP_ID_SETTLE_FIELD, "forged-app"),
    ],
)
def test_forged_frozen_app_markup_payout_is_invalid_without_credit(
    fake_store: tuple[Any, Any, Any], field: str, forged: Any
) -> None:
    store, db, _bt = fake_store
    ws = f"ws_apply_forged_{field}"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(store, workspace_id=ws, key_hash=key.hash)
    charge = 800_000
    payout = app_markup_owner_share_microdollars(
        app_markup_microdollars_from_charge(charge, auth.app_markup_basis_points)
    )
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            APP_MARKUP_PAYOUT_SETTLE_FIELD: payout,
            APP_MARKUP_OWNER_SETTLE_FIELD: auth.app_owner_user_id,
            APP_MARKUP_APP_ID_SETTLE_FIELD: auth.app_id,
            field: forged,
        }
    )

    assert (
        apply_frozen_settle(_row(auth, cost=charge, settle_body=json.dumps(body)))
        == ApplyOutcome.INVALID_ROW
    )
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert store.earnings_summary(auth.app_owner_user_id)["total_earned"] == 0


@pytest.mark.parametrize("intent,cost", [("refund", 0), ("settle", 0)])
def test_typed_app_markup_refund_and_zero_cost_have_no_payout(
    fake_store: tuple[Any, Any, Any], intent: str, cost: int
) -> None:
    store, db, _bt = fake_store
    ws = f"ws_apply_app_markup_{intent}"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_app_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, intent=intent, cost=cost)
    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert store.list_credit_movements("user:owner-app-markup") == []


@pytest.mark.parametrize("bad_payout", [-1, "5600"], ids=("negative", "non-int"))
def test_bad_frozen_user_model_payout_is_invalid_row(
    fake_store: tuple[Any, Any, Any],
    bad_payout: Any,
) -> None:
    store, db, _bt = fake_store
    ws = f"ws_apply_bad_user_model_payout_{type(bad_payout).__name__}"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    body = {
        "authorization_id": auth.id,
        "actual_input_tokens": 1_000,
        "actual_output_tokens": 2_000,
        USER_MODEL_PAYOUT_SETTLE_FIELD: bad_payout,
        USER_MODEL_OWNER_SETTLE_FIELD: "owner-user-model-payout",
        USER_MODEL_ID_SETTLE_FIELD: USER_MODEL_ID,
    }
    row = _row(
        auth,
        cost=8_000,
        endpoint_id=USER_MODEL_ENDPOINT_ID,
        model_id=USER_MODEL_ID,
        settle_body=json.dumps(body),
    )

    assert apply_frozen_settle(row) == ApplyOutcome.INVALID_ROW
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert store.earnings_summary("owner-user-model-payout")["total_earned"] == 0


def test_zero_cost_replay_is_benign(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_zero_replay"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, cost=0)

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert len(_generation_bodies(db)) == 1


def test_reaper_freed_reports_released_free(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_reaper"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    freed = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=auth.credit_reservation_id,
        actual_micro=0,
        settled_usage_type="Credits",
        success=False,
        guard_outbox=False,
    )
    assert freed["outcome"] == SettleOutcome.SETTLED

    assert apply_frozen_settle(_row(auth, cost=777_777)) == ApplyOutcome.ALREADY_RELEASED_FREE
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_credit(db, ws)["reserved"] == 0


def test_missing_reservation(fake_store: tuple[Any, Any, Any]) -> None:
    _store, _db, _bt = fake_store
    row = SettleOutboxRow(
        authorization_id="gwa-missing",
        intent_kind="settle",
        settle_origin="typed",
        actual_cost_micro=777_777,
        reservation_id="res-missing",
        selected_endpoint_id=ENDPOINT_ID,
        model_id=MODEL_ID,
        selected_usage_type="Credits",
        settle_body=_settle_body("gwa-missing"),
    )
    assert apply_frozen_settle(row) == ApplyOutcome.RESERVATION_MISSING


def test_typed_store_unavailable_parks(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_park"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    monkeypatch.setattr(apply_mod, "typed_billing_store", lambda: None)

    assert apply_frozen_settle(_row(auth, cost=777_777)) == ApplyOutcome.PARK_TYPED_UNAVAILABLE
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE


@pytest.mark.parametrize(
    "transient_exc",
    [
        pytest.param(Aborted("spanner aborted"), id="aborted"),
        pytest.param(DeadlineExceeded("spanner deadline"), id="deadline-exceeded"),
        pytest.param(InternalServerError("spanner internal"), id="internal-server-error"),
        pytest.param(ResourceExhausted("spanner exhausted"), id="resource-exhausted"),
        pytest.param(
            RetryError("retry exhausted", ServiceUnavailable("spanner down")),
            id="retry-error",
        ),
        pytest.param(ServiceUnavailable("spanner down"), id="service-unavailable"),
    ],
)
def test_transient_outage_parks_typed_row(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    transient_exc: Exception,
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_transient_typed"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    proxy = _TypedStoreProxy(
        store,
        finalize_exc=transient_exc,
    )
    monkeypatch.setattr(apply_mod, "typed_billing_store", lambda: proxy)

    assert apply_frozen_settle(_row(auth, cost=777_777)) == ApplyOutcome.PARK_TYPED_UNAVAILABLE
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE


def test_transient_pre_read_parks_typed_and_errors_legacy(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    typed_ws = "ws_apply_pre_read_typed"
    _seed_credit(store, typed_ws)
    typed_key = _make_key(store, typed_ws)
    typed_auth = _typed_authorization(store, workspace_id=typed_ws, key_hash=typed_key.hash)

    original_get_gateway_authorization = store.get_gateway_authorization

    def raise_unavailable(*_args: Any, **_kwargs: Any) -> GatewayAuthorization:
        raise ServiceUnavailable("spanner down")

    monkeypatch.setattr(store, "get_gateway_authorization", raise_unavailable)
    assert (
        apply_frozen_settle(_row(typed_auth, cost=777_777))
        == ApplyOutcome.PARK_TYPED_UNAVAILABLE
    )

    monkeypatch.setattr(store, "get_gateway_authorization", original_get_gateway_authorization)
    assert store.get_gateway_authorization(typed_auth.id).settled is False
    assert _typed_credit(db, typed_ws)["total_usage"] == 0
    assert _typed_credit(db, typed_ws)["reserved"] == ESTIMATE

    legacy_ws = "ws_apply_pre_read_legacy"
    _seed_credit(store, legacy_ws)
    legacy_key = _make_key(store, legacy_ws)
    legacy_auth = _legacy_authorization(
        store,
        workspace_id=legacy_ws,
        key_hash=legacy_key.hash,
    )

    monkeypatch.setattr(store, "get_gateway_authorization", raise_unavailable)
    assert apply_frozen_settle(_row(legacy_auth, origin="legacy")) == ApplyOutcome.ERROR

    monkeypatch.setattr(store, "get_gateway_authorization", original_get_gateway_authorization)
    assert store.get_gateway_authorization(legacy_auth.id).settled is False
    legacy_credit = store._read_entity("credit", legacy_ws, dict)
    assert legacy_credit.get("total_usage_microdollars", 0) == 0
    assert legacy_credit.get("reserved_microdollars", 0) == ESTIMATE


def test_transient_outage_errors_legacy_row(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _bt = fake_store
    ws = "ws_apply_transient_legacy"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _legacy_authorization(store, workspace_id=ws, key_hash=key.hash)

    def raiser(*_args: Any, **_kwargs: Any) -> bool:
        raise ServiceUnavailable("spanner down")

    monkeypatch.setattr(store, "finalize_gateway_authorization", raiser)

    assert apply_frozen_settle(_row(auth, origin="legacy")) == ApplyOutcome.ERROR
    assert store.get_gateway_authorization(auth.id).settled is False
    credit = store._read_entity("credit", ws, dict)
    assert credit.get("total_usage_microdollars", 0) == 0
    assert credit.get("reserved_microdollars", 0) == ESTIMATE


def test_legacy_replay_repairs_failed_app_markup_payout_exactly_once(
    fake_store: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _db, _bt = fake_store
    ws = "ws_apply_legacy_app_payout_repair"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _legacy_authorization(
        store,
        workspace_id=ws,
        key_hash=key.hash,
        app_id="legacy-app",
        app_markup_basis_points=500,
        app_owner_user_id="legacy-owner",
    )
    charge = 800_000
    payout = app_markup_owner_share_microdollars(
        app_markup_microdollars_from_charge(charge, auth.app_markup_basis_points)
    )
    body = json.loads(_settle_body(auth.id))
    body.update(
        {
            APP_MARKUP_PAYOUT_SETTLE_FIELD: payout,
            APP_MARKUP_OWNER_SETTLE_FIELD: auth.app_owner_user_id,
            APP_MARKUP_APP_ID_SETTLE_FIELD: auth.app_id,
        }
    )
    row = _row(auth, origin="legacy", cost=charge, settle_body=json.dumps(body))
    original_credit = store.credit_user_earnings
    finalize_calls = {"count": 0}

    def legacy_finalize(*_args: Any, **_kwargs: Any) -> bool:
        finalize_calls["count"] += 1
        return finalize_calls["count"] == 1

    def payout_failure(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("payout unavailable")

    monkeypatch.setattr(store, "finalize_gateway_authorization", legacy_finalize)
    monkeypatch.setattr(store, "credit_user_earnings", payout_failure)
    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert store.earnings_summary(auth.app_owner_user_id)["total_earned"] == 0

    monkeypatch.setattr(store, "credit_user_earnings", original_credit)
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_LEGACY
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_LEGACY
    assert store.earnings_summary(auth.app_owner_user_id)["total_earned"] == payout
    movements = store.list_credit_movements(f"user:{auth.app_owner_user_id}")
    assert [movement.movement_id for movement in movements].count(
        app_markup_payout_event_id(auth.id)
    ) == 1


def test_transient_disambiguation_read_parks_typed_row(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_disambiguation_transient"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, cost=777_777)

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    proxy = _TypedStoreProxy(
        store,
        finalize_result={"outcome": SettleOutcome.ALREADY_SETTLED},
        read_exc=ServiceUnavailable("spanner down"),
    )
    monkeypatch.setattr(apply_mod, "typed_billing_store", lambda: proxy)

    assert apply_frozen_settle(row) == ApplyOutcome.PARK_TYPED_UNAVAILABLE
    assert _typed_credit(db, ws)["total_usage"] == 777_777
    assert len(_generation_bodies(db)) == 1


def test_retired_endpoint_does_not_reprice_or_raise(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_retired"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(
        auth,
        cost=333_333,
        endpoint_id="ghost/model@nowhere/prepaid",
        model_id="ghost/model",
    )

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert _typed_credit(db, ws)["total_usage"] == 333_333
    generation = _generation_bodies(db)[0]
    assert generation["provider_name"] == "nowhere"
    assert generation["provider"] == "nowhere"
    assert generation["total_cost_microdollars"] == 333_333


def test_generation_parity_coerces_lenient_types(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_generation_body_parity"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    body = json.loads(_settle_body(auth.id))
    body["streamed"] = "false"
    row = _row(auth, settle_body=json.dumps(body))

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    generation = _generation_bodies(db)[0]
    assert generation["streamed"] is False


def test_invalid_settle_body_is_invalid_row(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_invalid"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    assert (
        apply_frozen_settle(_row(auth, settle_body="not json"))
        == ApplyOutcome.INVALID_ROW
    )
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE


def test_invalid_row_guards(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_invalid_guards"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    base = _row(auth)

    assert apply_frozen_settle(replace(base, intent_kind="bogus")) == ApplyOutcome.INVALID_ROW
    assert apply_frozen_settle(replace(base, selected_usage_type=None)) == ApplyOutcome.INVALID_ROW
    assert apply_frozen_settle(replace(base, settle_origin="weird")) == ApplyOutcome.INVALID_ROW
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE


def test_unvalidated_float_extra_is_invalid_row(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_invalid_extra"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    body = json.loads(_settle_body(auth.id))
    body["first_byte_seconds"] = "fast"

    row = _row(auth, settle_body=json.dumps(body))

    assert apply_frozen_settle(row) == ApplyOutcome.INVALID_ROW
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE


def test_refund_intent_releases_without_charge(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_refund"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    assert apply_frozen_settle(_row(auth, intent="refund", cost=777_777)) == ApplyOutcome.SETTLED_NOW
    assert _typed_credit(db, ws)["reserved"] == 0
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_key(db, key.hash)["reserved"] == 0
    assert _typed_key(db, key.hash)["usage"] == 0
    assert _generation_bodies(db) == []


def test_refund_replay_reports_released_free(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_refund_replay"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, intent="refund", cost=777_777)

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    credit_before = dict(_typed_credit(db, ws))
    key_before = dict(_typed_key(db, key.hash))
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_RELEASED_FREE
    assert _typed_credit(db, ws) == credit_before
    assert _typed_key(db, key.hash) == key_before
    assert _generation_bodies(db) == []


def test_error_outcome_passthrough(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws_apply_error_passthrough"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    proxy = _TypedStoreProxy(store, finalize_result={"outcome": SettleOutcome.ERROR})
    monkeypatch.setattr(apply_mod, "typed_billing_store", lambda: proxy)

    assert apply_frozen_settle(_row(auth, cost=777_777)) == ApplyOutcome.ERROR
    assert store.get_gateway_authorization(auth.id).settled is False
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE
