from __future__ import annotations

import json
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
from trusted_router.services import settle_outbox_apply as apply_mod
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome, SettleOutcome, settle_atomic
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_models import CreditAccount, GatewayAuthorization, SettleOutboxRow

MODEL_ID = "anthropic/claude-haiku-4.5"
PROVIDER = "anthropic"
ENDPOINT_ID = "anthropic/claude-haiku-4.5@anthropic/prepaid"
ESTIMATE = 1_000_000
TOTAL_CREDIT = 5_000_000


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
        CreditAccount(workspace_id=workspace_id, total_credits_microdollars=total),
    )


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


def _legacy_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int = ESTIMATE,
) -> GatewayAuthorization:
    reservation = store.reserve(workspace_id, key_hash, estimate)
    store.reserve_key_limit(key_hash, estimate, usage_type="Credits")
    return store.create_gateway_authorization(
        workspace_id=workspace_id,
        key_hash=key_hash,
        model_id=MODEL_ID,
        provider=PROVIDER,
        usage_type="Credits",
        estimated_microdollars=estimate,
        credit_reservation_id=reservation.id,
        requested_model_id=MODEL_ID,
        candidate_model_ids=[MODEL_ID],
        region="us",
        endpoint_id=ENDPOINT_ID,
        candidate_endpoint_ids=[ENDPOINT_ID],
    )


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
    return [
        json.loads(row.body)
        for (kind, _entity_id), row in db.rows.items()
        if kind == "generation"
    ]


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
    legacy_credit = store.get_credit_account(legacy_ws)
    assert legacy_credit.total_usage_microdollars == 0
    assert legacy_credit.reserved_microdollars == ESTIMATE


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
    credit = store.get_credit_account(ws)
    assert credit.total_usage_microdollars == 0
    assert credit.reserved_microdollars == ESTIMATE


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


def test_legacy_origin_applies_and_replays(fake_store: tuple[Any, Any, Any]) -> None:
    store, _db, _bt = fake_store
    ws = "ws_apply_legacy"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _legacy_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, origin="legacy", cost=654_321)

    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    credit = store.get_credit_account(ws)
    assert credit.total_usage_microdollars == 654_321
    assert credit.reserved_microdollars == 0
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_LEGACY
    assert store.get_credit_account(ws).total_usage_microdollars == 654_321


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
