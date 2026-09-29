from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.fakes.production_storage import PRODUCTION_SPANNER_STORAGE
from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    reap_expired_reservations,
    settle_atomic,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import (
    CreditAccount,
    Generation,
    SettleOutboxRow,
    Workspace,
)
from trusted_router.types import UsageType

MODEL_ID = "anthropic/claude-haiku-4.5"
PROVIDER = "anthropic"
ENDPOINT_ID = "anthropic/claude-haiku-4.5@anthropic/prepaid"
ESTIMATE = 1_000_000
TOTAL_CREDIT = 5_000_000


@pytest.fixture
def typed_request_store() -> Iterator[tuple[Any, Any]]:
    store, database = make_fake_store(request_record_write_mode="typed")
    configure_store(store)
    try:
        yield store, database
    finally:
        configure_store(InMemoryStore())


def _seed_credit(store: Any, workspace_id: str) -> None:
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id),
    )
    store._database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace_id, 0)] = {
        "workspace_id": workspace_id,
        "shard": 0,
        "total_credits": TOTAL_CREDIT,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }


def _make_key(store: Any, workspace_id: str) -> Any:
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="primary",
        creator_user_id=None,
        limit_microdollars=TOTAL_CREDIT,
    )
    return key


def _authorize(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    idempotency_key: str | None = None,
    idempotency_fingerprint: str | None = None,
    expires_at: str = "2099-01-01T00:00:00Z",
) -> tuple[str, Any]:
    return store.authorize_gateway_typed(
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
        idempotency_key=idempotency_key,
        idempotency_fingerprint=idempotency_fingerprint,
        expires_at=expires_at,
    )


def _settle_body(authorization_id: str) -> dict[str, Any]:
    return {
        "authorization_id": authorization_id,
        "gateway_request_id": "rlog_00112233445566778899aabbccddeeff",
        "actual_input_tokens": 14,
        "actual_output_tokens": 7,
        "request_id": "req-retention-test",
        "finish_reason": "stop",
        "status": "success",
        "streamed": True,
        "elapsed_seconds": 2.0,
        "selected_model": MODEL_ID,
        "selected_endpoint": ENDPOINT_ID,
    }


def _outbox_row(authorization: Any, intent_kind: str) -> SettleOutboxRow:
    return SettleOutboxRow(
        authorization_id=authorization.id,
        intent_kind=intent_kind,
        settle_origin="typed",
        actual_cost_micro=0 if intent_kind == "refund" else 777_777,
        reservation_id=authorization.credit_reservation_id,
        selected_endpoint_id=ENDPOINT_ID,
        model_id=MODEL_ID,
        selected_usage_type="Credits",
        settle_body=json.dumps(_settle_body(authorization.id)),
    )


def _outbox(store: Any) -> SpannerSettleOutbox:
    return SpannerSettleOutbox(store._database, store._param_types)


def _client() -> TestClient:
    settings = Settings(environment="test", settle_outbox_enabled=True)
    return TestClient(
        create_app(settings, configure_store_arg=False, init_observability=False)
    )


def _generic_request_kinds(database: Any) -> set[str]:
    request_kinds = {
        "gateway_authorization",
        "gateway_authorization_idempotency",
        "generation",
        "generation_by_workspace",
    }
    return {kind for kind, _entity_id in database.rows if kind in request_kinds}


def test_typed_authorize_avoids_generic_rows_and_replays_one_hold(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-typed-authorize"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)

    first_outcome, first = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
        idempotency_key="same-request",
        idempotency_fingerprint="fingerprint",
    )
    replay_outcome, replay = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
        idempotency_key="same-request",
        idempotency_fingerprint="fingerprint",
    )

    assert first_outcome == AuthorizeOutcome.ACCEPTED
    assert replay_outcome == AuthorizeOutcome.REPLAY
    assert first is not None and replay is not None
    assert replay.id == first.id
    assert len(database.reservations) == 1
    assert len(database.gateway_authorizations) == 1
    assert database.gateway_authorizations[first.id]["terminal_at"] is None
    assert _generic_request_kinds(database) == set()


def test_shared_trace_settles_each_call_once_and_releases_both_holds(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-shared-trace"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    authorizations = []
    for _ in range(2):
        outcome, authorization = _authorize(
            store, workspace_id=workspace_id, key_hash=key.hash,
        )
        assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
        authorizations.append(authorization)

    client = _client()
    for authorization in authorizations:
        response = client.post(
            "/v1/internal/gateway/settle", json=_settle_body(authorization.id),
        )
        assert response.status_code == 200, response.text
    balances_before_replay = copy.deepcopy(database.typed)
    for authorization in reversed(authorizations):
        response = client.post(
            "/v1/internal/gateway/settle", json=_settle_body(authorization.id),
        )
        assert response.status_code == 200, response.text
        assert database.reservations[authorization.credit_reservation_id]["settled"]
        assert database.settle_outbox[(authorization.id, "settle")]["status"] == "done"
    assert database.typed == balances_before_replay
    credit = database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]
    assert credit["reserved"] == 0
    assert credit["total_usage"] == sum(
        database.reservations[authorization.credit_reservation_id]["actual_micro"]
        for authorization in authorizations
    )
    assert len({
        database.gateway_authorizations[authorization.id]["gateway_request_id"]
        for authorization in authorizations
    }) == 1


def test_typed_settle_starts_bounded_replay_window_after_activity_commit(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-typed-settle"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None

    response = _client().post(
        "/v1/internal/gateway/settle",
        json=_settle_body(authorization.id),
    )

    assert response.status_code == 200, response.text
    auth_row = database.gateway_authorizations[authorization.id]
    reservation = database.reservations[authorization.credit_reservation_id]
    outbox = database.settle_outbox[(authorization.id, "settle")]
    assert auth_row["settled"] is True
    assert auth_row["gateway_request_id"] == (
        "rlog_00112233445566778899aabbccddeeff"
    )
    assert auth_row["payload"] is not None
    assert auth_row["terminal_at"] is not None
    assert reservation["settled"] is True
    assert database.reservations[authorization.credit_reservation_id][
        "terminal_at"
    ] is not None
    assert outbox["status"] == "done"
    assert outbox["settle_body"] is None
    assert outbox["terminal_at"] is not None
    assert _generic_request_kinds(database) == set()


def test_settled_idempotency_key_replays_for_full_retention_window(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-typed-settled-replay"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
        idempotency_key="settled-replay",
        idempotency_fingerprint="same-request",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None

    response = _client().post(
        "/v1/internal/gateway/settle",
        json=_settle_body(authorization.id),
    )
    assert response.status_code == 200, response.text

    replay_outcome, replay = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
        idempotency_key="settled-replay",
        idempotency_fingerprint="same-request",
    )
    mismatch_outcome, mismatch = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
        idempotency_key="settled-replay",
        idempotency_fingerprint="different-request",
    )

    assert replay_outcome == AuthorizeOutcome.REPLAY
    assert replay is not None and replay.id == authorization.id
    assert replay.candidate_endpoint_ids == [ENDPOINT_ID]
    assert mismatch_outcome == AuthorizeOutcome.IDEMPOTENCY_MISMATCH
    assert mismatch is None
    assert len(database.reservations) == 1
    assert len(database.gateway_authorizations) == 1
    assert database.gateway_authorizations[authorization.id]["payload"] is not None


def test_gateway_route_replays_settled_request_from_bounded_record(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-route-settled-replay"
    store._write_entity(
        "workspace",
        workspace_id,
        Workspace(id=workspace_id, name="Replay", owner_user_id="user-replay"),
    )
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    client = _client()
    authorize_body = {
        "api_key_hash": key.hash,
        "idempotency_key": "route-settled-replay",
        "model": MODEL_ID,
        "estimated_input_tokens": 14,
        "max_output_tokens": 7,
    }

    first = client.post("/v1/internal/gateway/authorize", json=authorize_body)
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    settled = client.post(
        "/v1/internal/gateway/settle",
        json=_settle_body(first_data["authorization_id"]),
    )
    assert settled.status_code == 200, settled.text

    replay = client.post("/v1/internal/gateway/authorize", json=authorize_body)

    assert replay.status_code == 200, replay.text
    replay_data = replay.json()["data"]
    assert replay_data["authorization_id"] == first_data["authorization_id"]
    assert replay_data["route_candidates"] == first_data["route_candidates"]
    assert replay_data["idempotent_replay"] is True
    assert len(database.reservations) == 1
    assert len(database.gateway_authorizations) == 1


def test_typed_enqueue_failure_rejects_without_charging(
    typed_request_store: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-typed-enqueue-failure"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None

    def fail_enqueue(
        self: SpannerSettleOutbox,
        row: Any,
        *,
        initial_delay_seconds: int = 0,
    ) -> str:
        _ = self, row, initial_delay_seconds
        raise RuntimeError("injected outbox outage")

    monkeypatch.setattr(SpannerSettleOutbox, "enqueue", fail_enqueue)
    response = _client().post(
        "/v1/internal/gateway/settle",
        json=_settle_body(authorization.id),
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
    reservation = database.reservations[authorization.credit_reservation_id]
    assert reservation["settled"] is False
    assert reservation["terminal_at"] is None
    assert database.gateway_authorizations[authorization.id]["settled"] is False
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)][
        "total_usage"
    ] == 0
    assert _outbox(store).get(authorization.id, "settle") is None


def test_legacy_rolling_finalize_defers_retention_until_outbox_done() -> None:
    store, database = make_fake_store(
        request_record_write_mode="legacy"
    )
    workspace_id = "ws-legacy-finalize-retention"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None
    outbox = _outbox(store)
    assert outbox.enqueue(_outbox_row(authorization, "settle")) == "inserted"

    finalized = store.typed_finalize_gateway_authorization_result(
        authorization.id,
        success=True,
        actual_microdollars=777_777,
        selected_usage_type=UsageType.CREDITS,
    )

    assert finalized.finalized is True
    assert finalized.request_record_typed is False
    assert database.reservations[reservation_id]["settled"] is True
    assert database.reservations[reservation_id]["terminal_at"] is None

    assert outbox.mark(authorization.id, "settle", done=True) == "done"
    assert database.reservations[reservation_id]["terminal_at"] is not None


@pytest.mark.parametrize("outbox_status", ["pending", "dead"])
def test_claim_with_outstanding_intent_defers_retention(
    typed_request_store: tuple[Any, Any],
    outbox_status: str,
) -> None:
    store, database = typed_request_store
    workspace_id = f"ws-claim-retention-{outbox_status}"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None
    outbox = _outbox(store)
    assert outbox.enqueue(_outbox_row(authorization, "settle")) == "inserted"
    database.settle_outbox[(authorization.id, "settle")][
        "status"
    ] = outbox_status

    # Bypass the MF2 reaper guard to exercise the claim's structural column
    # guard: the claim/release still wins, but retention must remain deferred.
    result = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=reservation_id,
        actual_micro=0,
        settled_usage_type="Credits",
        success=False,
        guard_outbox=False,
    )

    assert result["outcome"] == SettleOutcome.SETTLED
    assert database.reservations[reservation_id]["settled"] is True
    assert database.reservations[reservation_id]["terminal_at"] is None


def test_settle_without_outbox_still_arms_retention(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-no-outbox-retention"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None
    assert database.settle_outbox == {}

    result = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=reservation_id,
        actual_micro=777_777,
        settled_usage_type="Credits",
        success=True,
        guard_outbox=False,
    )

    assert result["outcome"] == SettleOutcome.SETTLED
    assert database.settle_outbox == {}
    assert database.reservations[reservation_id]["terminal_at"] is not None


def test_typed_reaper_compacts_unresolved_authorization(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-typed-reaper"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
        expires_at="2000-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None

    reaped = reap_expired_reservations(
        store._database,
        store._param_types,
        now="2026-07-27T00:00:00Z",
    )

    assert reaped == 1
    reservation = database.reservations[authorization.credit_reservation_id]
    auth_row = database.gateway_authorizations[authorization.id]
    assert reservation["settled"] is True
    assert reservation["actual_micro"] == 0
    assert reservation["terminal_at"] is not None
    assert auth_row["settled"] is True
    assert auth_row["payload"] is not None
    payload = json.loads(auth_row["payload"])
    assert payload["id"] == authorization.id
    assert payload["credit_reservation_id"] == authorization.credit_reservation_id
    assert payload["finalization_outcome"] == "refunded"
    assert auth_row["terminal_at"] is not None


def test_late_outbox_enqueue_disarms_reaper_retention(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-reaper-late-settle"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
        expires_at="2000-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None

    reaped = reap_expired_reservations(
        store._database,
        store._param_types,
        now="2026-07-27T00:00:00Z",
    )
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None
    assert reaped == 1
    assert database.reservations[reservation_id]["terminal_at"] is not None
    assert database.gateway_authorizations[authorization.id][
        "terminal_at"
    ] is not None

    assert _outbox(store).enqueue(_outbox_row(authorization, "settle")) == "inserted"

    pending = _outbox(store).get(authorization.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.terminal_at is None
    assert database.reservations[reservation_id]["terminal_at"] is None
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is None


def test_retention_waits_for_last_sibling_outbox_intent(
    typed_request_store: tuple[Any, Any],
) -> None:
    store, database = typed_request_store
    workspace_id = "ws-sibling-outbox-retention"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None
    outbox = _outbox(store)
    assert outbox.enqueue(_outbox_row(authorization, "settle")) == "inserted"
    assert outbox.enqueue(_outbox_row(authorization, "refund")) == "inserted"

    response = _client().post(
        "/v1/internal/gateway/settle",
        json=_settle_body(authorization.id),
    )

    assert response.status_code == 200, response.text
    settled = outbox.get(authorization.id, "settle")
    refund = outbox.get(authorization.id, "refund")
    assert settled is not None and settled.status == "done"
    assert refund is not None and refund.status == "pending"
    assert database.reservations[reservation_id]["terminal_at"] is None
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is None

    assert outbox.mark(authorization.id, "refund", done=True) == "done"
    assert database.reservations[reservation_id]["terminal_at"] is not None
    assert database.gateway_authorizations[authorization.id][
        "terminal_at"
    ] is not None


def _generation(generation_id: str, *, cost: int) -> Generation:
    return Generation(
        id=generation_id,
        request_id=f"req-{generation_id}",
        workspace_id="ws-family-read",
        key_hash="key-family-read",
        model=MODEL_ID,
        provider_name="Anthropic",
        app="Family read test",
        tokens_prompt=2,
        tokens_completion=1,
        total_cost_microdollars=cost,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=1.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        provider=PROVIDER,
        created_at="2026-07-27T12:00:00Z",
    )


def test_typed_production_mode_requires_durable_outbox() -> None:
    internal_token = "internal-" + "token"
    with pytest.raises(
        ValidationError,
        match="TR_SETTLE_OUTBOX_ENABLED=true",
    ):
        Settings(
            environment="production",
            service_surface="internal",
            internal_gateway_token=internal_token,
            observer_internal_token="observer-" + "token",
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            **{**PRODUCTION_SPANNER_STORAGE, "settle_outbox_enabled": False},
            byok_kms_key_name="projects/p/locations/global/keyRings/r/cryptoKeys/k",
        )


def test_dead_outbox_row_disarms_claim_armed_retention(
    typed_request_store: tuple[Any, Any],
) -> None:
    """A winning claim arms terminal_at at settle time (settle_atomic sets it on
    the reservation). If that authorization's outbox row later goes dead — repair
    unfinished, frozen for a human — the referenced records must be disarmed
    again, or the 30-day TTL deletes the very evidence the freeze preserves."""
    store, database = typed_request_store
    workspace_id = "ws-dead-row-retention"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None

    outbox = _outbox(store)
    assert outbox.enqueue(_outbox_row(authorization, "settle")) == "inserted"

    # Stand in for the winning claim, which arms retention while the outbox row
    # is still pending.
    database.reservations[reservation_id]["terminal_at"] = "2026-07-27T00:00:00Z"
    database.gateway_authorizations[authorization.id][
        "terminal_at"
    ] = "2026-07-27T00:00:00Z"

    assert (
        outbox.mark(authorization.id, "settle", done=False, force_dead=True)
        == "dead"
    )

    dead = outbox.get(authorization.id, "settle")
    assert dead is not None and dead.status == "dead"
    assert dead.terminal_at is None
    assert database.reservations[reservation_id]["terminal_at"] is None
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is None


def test_parked_outbox_row_disarms_claim_armed_retention(
    typed_request_store: tuple[Any, Any],
) -> None:
    """park() keeps an intent outstanding without burning attempts, and it is
    reached AFTER a winning claim may have armed terminal_at (e.g. the settle
    committed but its activity index has not). A repair that parks for 30 days
    must not let the TTL delete the records it is repairing."""
    store, database = typed_request_store
    workspace_id = "ws-parked-retention"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None

    outbox = _outbox(store)
    assert outbox.enqueue(_outbox_row(authorization, "settle")) == "inserted"

    database.reservations[reservation_id]["terminal_at"] = "2026-07-27T00:00:00Z"
    database.gateway_authorizations[authorization.id][
        "terminal_at"
    ] = "2026-07-27T00:00:00Z"

    assert outbox.park(authorization.id, "settle", lease_owner=None) is True

    parked = outbox.get(authorization.id, "settle")
    assert parked is not None and parked.status == "pending"
    assert database.reservations[reservation_id]["terminal_at"] is None
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is None


def test_sibling_completion_clears_already_armed_retention(
    typed_request_store: tuple[Any, Any],
) -> None:
    """Skipping the arm is not enough when terminal_at was ALREADY armed after
    enqueue (a winning claim does that): completing one intent while a sibling
    is still pending must actively disarm the shared records."""
    store, database = typed_request_store
    workspace_id = "ws-sibling-prearmed"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    reservation_id = authorization.credit_reservation_id
    assert reservation_id is not None

    outbox = _outbox(store)
    assert outbox.enqueue(_outbox_row(authorization, "settle")) == "inserted"
    assert outbox.enqueue(_outbox_row(authorization, "refund")) == "inserted"

    database.reservations[reservation_id]["terminal_at"] = "2026-07-27T00:00:00Z"
    database.gateway_authorizations[authorization.id][
        "terminal_at"
    ] = "2026-07-27T00:00:00Z"

    assert outbox.mark(authorization.id, "settle", done=True) == "done"

    assert outbox.get(authorization.id, "refund").status == "pending"
    assert database.reservations[reservation_id]["terminal_at"] is None
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is None
