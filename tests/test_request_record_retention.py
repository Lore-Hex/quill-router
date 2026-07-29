from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import settle_outbox_drain as drain_mod
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_activity_index import (
    generation_by_id,
    write_generation,
)
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    reap_expired_reservations,
    settle_atomic,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_gcp_synthetic_index import (
    RETENTION_FAMILY_MAX_AGES,
    configure_retention_families,
)
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
def typed_request_store() -> Iterator[tuple[Any, Any, Any]]:
    store, database, bigtable = make_fake_store(request_record_write_mode="typed")
    configure_store(store)
    try:
        yield store, database, bigtable
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
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, _bigtable = typed_request_store
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


def test_typed_settle_starts_bounded_replay_window_after_activity_commit(
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, bigtable = typed_request_store
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
    assert any(
        "activity" in cells
        for row_key, cells in bigtable.rows.items()
        if row_key.startswith(b"gen#")
    )
    assert any(
        "benchmark" in cells
        for row_key, cells in bigtable.rows.items()
        if row_key.startswith(b"benchmark")
    )
    assert all("m" not in cells for cells in bigtable.rows.values())


def test_settled_idempotency_key_replays_for_full_retention_window(
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, _bigtable = typed_request_store
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
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, _bigtable = typed_request_store
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


def test_bigtable_failure_preserves_private_repair_state_until_retry(
    typed_request_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp_generations as generations_mod

    store, database, bigtable = typed_request_store
    workspace_id = "ws-typed-repair"
    _seed_credit(store, workspace_id)
    key = _make_key(store, workspace_id)
    outcome, authorization = _authorize(
        store,
        workspace_id=workspace_id,
        key_hash=key.hash,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED and authorization is not None
    original_write = generations_mod._bt_write_generation
    calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected Bigtable outage")
        original_write(*args, **kwargs)

    monkeypatch.setattr(generations_mod, "_bt_write_generation", fail_once)
    body = _settle_body(authorization.id)
    body.update(
        {
            "prompt": "private prompt must not persist",
            "output": "private output must not persist",
            "tool_calls": [{"function": {"arguments": "private tool output"}}],
            "metadata": {
                "trustedrouter_synthetic": "true",
                "private": "private metadata must not persist",
            },
            "trace": {"private": "private trace must not persist"},
        }
    )

    response = _client().post("/v1/internal/gateway/settle", json=body)

    assert response.status_code == 200, response.text
    auth_row = database.gateway_authorizations[authorization.id]
    reservation = database.reservations[authorization.credit_reservation_id]
    pending = _outbox(store).get(authorization.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.terminal_at is None
    assert pending.settle_body is not None
    assert auth_row["settled"] is True
    assert auth_row["terminal_at"] is None
    assert auth_row["payload"] is not None
    assert reservation["settled"] is True
    assert reservation.get("terminal_at") is None
    frozen = json.loads(pending.settle_body)
    assert frozen["metadata"] == {"trustedrouter_synthetic": "true"}
    durable_state = json.dumps(
        {
            "authorization": auth_row,
            "reservation": reservation,
            "outbox": database.settle_outbox[(authorization.id, "settle")],
        },
        default=str,
    )
    for secret in (
        "private prompt",
        "private output",
        "private tool output",
        "private metadata",
        "private trace",
    ):
        assert secret not in durable_state

    database.settle_outbox[(authorization.id, "settle")][
        "next_attempt_at"
    ] = "2000-01-01T00:00:00Z"
    drained = drain_mod.drain_settle_outbox(10)

    assert drained["outcomes"] == {
        ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE: 1
    }
    completed = _outbox(store).get(authorization.id, "settle")
    assert completed is not None and completed.status == "done"
    assert completed.settle_body is None
    assert completed.terminal_at is not None
    assert database.gateway_authorizations[authorization.id]["payload"] is not None
    assert database.gateway_authorizations[authorization.id]["terminal_at"] is not None
    assert database.reservations[authorization.credit_reservation_id][
        "terminal_at"
    ] is not None
    generation_keys = {
        key for key in bigtable.rows if key.startswith(b"gen#")
    }
    assert len(generation_keys) == 1
    assert calls == 2
    assert _generic_request_kinds(database) == set()


def test_typed_enqueue_failure_rejects_without_charging(
    typed_request_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, _bigtable = typed_request_store
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
    store, database, _bigtable = make_fake_store(
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
    typed_request_store: tuple[Any, Any, Any],
    outbox_status: str,
) -> None:
    store, database, _bigtable = typed_request_store
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
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, _bigtable = typed_request_store
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
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, _bigtable = typed_request_store
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
    assert auth_row["payload"] is None
    assert auth_row["terminal_at"] is not None


def test_late_outbox_enqueue_disarms_reaper_retention(
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, _bigtable = typed_request_store
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
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    store, database, _bigtable = typed_request_store
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


def test_bigtable_reads_prefer_bounded_family_and_fall_back_to_legacy(
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    _store, _database, bigtable = typed_request_store
    legacy = _generation("gen-shared", cost=1)
    bounded = _generation("gen-shared", cost=2)
    legacy_only = _generation("gen-legacy-only", cost=3)
    write_generation(bigtable, "m", legacy)
    write_generation(bigtable, "activity", bounded)
    write_generation(bigtable, "m", legacy_only)

    preferred = generation_by_id(
        bigtable,
        ("activity", "m"),
        "gen-shared",
    )
    fallback = generation_by_id(
        bigtable,
        ("activity", "m"),
        "gen-legacy-only",
    )

    assert preferred is not None
    assert preferred.total_cost_microdollars == 2
    assert fallback is not None
    assert fallback.total_cost_microdollars == 3


def test_typed_production_mode_requires_durable_outbox() -> None:
    internal_token = "internal-" + "token"
    webhook_secret = "whsec_" + "test"
    stripe_secret = "sk_" + "test"
    with pytest.raises(
        ValidationError,
        match="TR_SETTLE_OUTBOX_ENABLED=true",
    ):
        Settings(
            environment="production",
            internal_gateway_token=internal_token,
            stripe_webhook_secret=webhook_secret,
            stripe_secret_key=stripe_secret,
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            storage_backend="spanner-bigtable",
            spanner_instance_id="trusted-router",
            spanner_database_id="trusted-router",
            bigtable_instance_id="trusted-router-logs",
            byok_kms_key_name="projects/p/locations/global/keyRings/r/cryptoKeys/k",
            request_record_write_mode="typed",
            settle_outbox_enabled=False,
        )


class _FakeFamily:
    def __init__(self, name: str, calls: list[tuple[str, str]]) -> None:
        self.name = name
        self.calls = calls

    def create(self) -> None:
        self.calls.append(("create", self.name))

    def update(self) -> None:
        self.calls.append(("update", self.name))


class _FakeAdminTable:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def list_column_families(self) -> dict[str, object]:
        return {"activity": object(), "m": object()}

    def column_family(self, name: str, *, gc_rule: Any) -> _FakeFamily:
        assert gc_rule is not None
        return _FakeFamily(name, self.calls)


def test_bigtable_retention_config_never_mutates_legacy_family() -> None:
    table = _FakeAdminTable()

    actions = configure_retention_families(table, apply=True)

    assert {action["family"] for action in actions} == set(
        RETENTION_FAMILY_MAX_AGES
    )
    assert table.calls == [
        ("update", "activity"),
        ("create", "benchmark"),
        ("create", "synthetic"),
        ("create", "rollup"),
    ]
    assert all(name != "m" for _action, name in table.calls)


def test_retention_migration_is_additive_and_dry_run_by_default() -> None:
    script = (
        Path(__file__).parents[1]
        / "scripts"
        / "deploy"
        / "migrate_request_retention.sh"
    ).read_text()
    executable_lines = [
        line.strip().lower()
        for line in script.splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
        and not line.lstrip().startswith("log ")
    ]

    assert "apply=false" in executable_lines
    assert any('if [ "${1:-}" = "--apply" ]' in line for line in executable_lines)
    assert not any("delete from" in line for line in executable_lines)
    assert not any("drop table" in line for line in executable_lines)
    assert not any("update tr_" in line and "terminal_at" in line for line in executable_lines)


def test_dead_outbox_row_disarms_claim_armed_retention(
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    """A winning claim arms terminal_at at settle time (settle_atomic sets it on
    the reservation). If that authorization's outbox row later goes dead — repair
    unfinished, frozen for a human — the referenced records must be disarmed
    again, or the 30-day TTL deletes the very evidence the freeze preserves."""
    store, database, _bigtable = typed_request_store
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
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    """park() keeps an intent outstanding without burning attempts, and it is
    reached AFTER a winning claim may have armed terminal_at (e.g. the settle
    committed but its activity index has not). A repair that parks for 30 days
    must not let the TTL delete the records it is repairing."""
    store, database, _bigtable = typed_request_store
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
    typed_request_store: tuple[Any, Any, Any],
) -> None:
    """Skipping the arm is not enough when terminal_at was ALREADY armed after
    enqueue (a winning claim does that): completing one intent while a sibling
    is still pending must actively disarm the shared records."""
    store, database, _bigtable = typed_request_store
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
