from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import asdict, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import DeadlineExceeded

from tests.fakes.spanner import _FakeTransaction, make_fake_store
from trusted_router.app_markup_billing import (
    APP_MARKUP_PAYOUT_SETTLE_FIELD,
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
)
from trusted_router.config import Settings
from trusted_router.custom_model_billing import user_model_payout_event_id
from trusted_router.main import create_app
from trusted_router.regional_quota_ledger import RegionalLeaseLedgerError
from trusted_router.services import auto_refill as auto_refill_mod
from trusted_router.services import auto_refill_outbox_drain as auto_refill_drain_mod
from trusted_router.services import settle_outbox_apply as apply_mod
from trusted_router.services import settle_outbox_drain as drain_mod
from trusted_router.services.auto_refill import AutoRefillOutcome
from trusted_router.services.auto_refill_outbox_drain import AutoRefillDrainPass
from trusted_router.services.regional_quota_leases import LeaseSettlementError
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    reap_expired_reservations,
    settle_atomic,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import (
    CreditAccount,
    GatewayAuthorization,
    SettleOutboxRow,
    TypedFinalizeResult,
)

MODEL_ID = "anthropic/claude-haiku-4.5"
PROVIDER = "anthropic"
ENDPOINT_ID = "anthropic/claude-haiku-4.5@anthropic/prepaid"
USER_MODEL_ID = "tr-user-model/owner-outbox-repair"
USER_MODEL_ENDPOINT_ID = f"{USER_MODEL_ID}@trustedrouter/credits"
ESTIMATE = 1_000_000
TOTAL_CREDIT = 5_000_000
NOW = "2026-07-04T12:00:00Z"
EXPIRED_AT = "2000-01-01T00:00:00Z"
GATEWAY_LOGGER = "trusted_router.routes.internal.gateway"
TIMING_FIELDS = ("total_ms", "auth_ms", "enqueue_ms", "finalize_ms", "mark_ms")


@pytest.fixture
def fake_store() -> Iterator[tuple[Any, Any, Any]]:
    store, db, bt = make_fake_store()
    configure_store(store)
    try:
        yield store, db, bt
    finally:
        configure_store(InMemoryStore())


@pytest.fixture
def prod_shaped_store() -> Iterator[tuple[Any, Any, Any]]:
    """The store as production runs it: the ClickHouse delivery intent is
    written INSIDE the finalize transaction (operational analytics outbox on,
    typed request records). That in-commit durability is what allows the
    settle-outbox done-mark to be folded into the same commit."""
    store, db, bt = make_fake_store(
        operational_analytics_outbox_enabled=True,
        request_record_write_mode="typed",
        generation_records_enabled=True,
    )
    configure_store(store)
    try:
        yield store, db, bt
    finally:
        configure_store(InMemoryStore())


def _client(settings: Settings, *, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(
        create_app(settings, configure_store_arg=False, init_observability=False),
        raise_server_exceptions=raise_server_exceptions,
    )


def _outbox(store: Any) -> SpannerSettleOutbox:
    return SpannerSettleOutbox(store._database, store._param_types)


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


def _generation_count(db: Any) -> int:
    return sum(1 for (kind, _entity_id) in db.rows if kind == "generation")


def _typed_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int = ESTIMATE,
    expires_at: str = "2026-01-01T00:00:00Z",
    app_id: str = "",
    app_markup_basis_points: int = 0,
    app_owner_user_id: str = "",
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
        app_id=app_id,
        app_markup_basis_points=app_markup_basis_points,
        app_owner_user_id=app_owner_user_id,
        expires_at=expires_at,
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert auth is not None
    return auth


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
        idempotency_key="typed-user-model-outbox-repair",
        idempotency_fingerprint="typed-user-model-outbox-repair-fingerprint",
        user_provided_model_id=USER_MODEL_ID,
        user_provided_model_revision=4,
        user_model_prompt_price_microdollars_per_m=2_000_000,
        user_model_completion_price_microdollars_per_m=3_000_000,
        user_model_owner_user_id="owner-outbox-repair",
        expires_at="2026-01-01T00:00:00Z",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert auth is not None
    return auth


def _expired_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int = ESTIMATE,
) -> GatewayAuthorization:
    return _typed_authorization(
        store,
        workspace_id=workspace_id,
        key_hash=key_hash,
        estimate=estimate,
        expires_at=EXPIRED_AT,
    )


def _legacy_authorization(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int = ESTIMATE,
) -> GatewayAuthorization:
    reservation_id = f"legacy-res-{workspace_id}-{key_hash}"
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
    )


def _bare_authorization(auth_id: str) -> GatewayAuthorization:
    return GatewayAuthorization(
        id=auth_id,
        workspace_id=f"ws-{auth_id}",
        key_hash=f"key-{auth_id}",
        model_id=MODEL_ID,
        provider=PROVIDER,
        usage_type="Credits",
        estimated_microdollars=ESTIMATE,
        credit_reservation_id=f"res-{auth_id}",
    )


def _settle_json(auth_id: str, *, request_id: str = "req-settle") -> dict[str, Any]:
    return {
        "authorization_id": auth_id,
        "actual_input_tokens": 14,
        "actual_output_tokens": 7,
        "cache_read_input_tokens": 6_081,
        "cache_creation_input_tokens": 2,
        "request_id": request_id,
        "finish_reason": "stop",
        "status": "success",
        "streamed": True,
        "elapsed_seconds": 2.0,
        "selected_model": MODEL_ID,
        "selected_endpoint": ENDPOINT_ID,
    }


def _zero_cost_settle_json(auth_id: str) -> dict[str, Any]:
    body = _settle_json(auth_id)
    body.update(
        actual_input_tokens=0,
        actual_output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return body


def _row(
    auth: GatewayAuthorization,
    *,
    intent: str = "settle",
    origin: str = "typed",
    cost: int = 777_777,
    selected_usage_type: str | None = "Credits",
    settle_body: str | None = None,
) -> SettleOutboxRow:
    return SettleOutboxRow(
        authorization_id=auth.id,
        intent_kind=intent,
        settle_origin=origin,
        actual_cost_micro=cost,
        reservation_id=auth.credit_reservation_id,
        selected_endpoint_id=ENDPOINT_ID,
        model_id=MODEL_ID,
        selected_usage_type=selected_usage_type,
        settle_body=settle_body if settle_body is not None else json.dumps(_settle_json(auth.id)),
    )


def _settle_timing_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == GATEWAY_LOGGER and record.getMessage().startswith("settle timing ")
    ]


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
        "spend_lease_id": "lease-settle-clamp",
        "spend_lease_gen": 7,
        "spend_lease_allocated_micro": allocation_micro,
    }
    payload.update(binding)
    record.update(binding)
    record["payload"] = json.dumps(payload, separators=(",", ":"))


# Unit (storage)


def test_park_leaves_attempts_unchanged_and_respects_lease(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, _db, _bt = fake_store
    ob = _outbox(store)
    row = _row(
        GatewayAuthorization(
            id="gwa-park",
            workspace_id="ws-park",
            key_hash="key-park",
            model_id=MODEL_ID,
            provider=PROVIDER,
            usage_type="Credits",
            estimated_microdollars=ESTIMATE,
            credit_reservation_id="res-park",
        )
    )
    ob.enqueue(row)
    [job] = ob.claim(lease_seconds=300)
    assert ob.park(row.authorization_id, "settle", lease_owner="soworker_intruder") is False
    before = ob.get(row.authorization_id, "settle")
    assert before is not None and before.attempts == 0 and before.lease_owner == job.lease_owner

    assert ob.park(
        row.authorization_id,
        "settle",
        lease_owner=job.lease_owner,
        retry_after_seconds=120,
        note="typed store unavailable",
    )
    after = ob.get(row.authorization_id, "settle")
    assert after is not None
    assert after.status == "pending"
    assert after.attempts == 0
    assert after.lease_owner is None and after.leased_until is None
    assert after.last_error == "typed store unavailable"
    assert after.next_attempt_at != before.next_attempt_at


def test_mark_force_dead_goes_dead_immediately(fake_store: tuple[Any, Any, Any]) -> None:
    store, _db, _bt = fake_store
    ob = _outbox(store)
    auth = GatewayAuthorization(
        id="gwa-force-dead",
        workspace_id="ws-force-dead",
        key_hash="key-force-dead",
        model_id=MODEL_ID,
        provider=PROVIDER,
        usage_type="Credits",
        estimated_microdollars=ESTIMATE,
        credit_reservation_id="res-force-dead",
    )
    ob.enqueue(_row(auth))

    assert ob.mark(auth.id, "settle", done=False, force_dead=True, error="invalid") == "dead"
    got = ob.get(auth.id, "settle")
    assert got is not None
    assert got.status == "dead"
    assert got.attempts == 1
    assert got.last_error == "invalid"


def test_fake_requires_park_and_typed_dml_predicates(fake_store: tuple[Any, Any, Any]) -> None:
    store, _db, _bt = fake_store
    auth = GatewayAuthorization(
        id="gwa-mf6",
        workspace_id="ws-mf6",
        key_hash="key-mf6",
        model_id=MODEL_ID,
        provider=PROVIDER,
        usage_type="Credits",
        estimated_microdollars=ESTIMATE,
        credit_reservation_id="res-mf6",
    )
    _outbox(store).enqueue(_row(auth))
    with pytest.raises(AssertionError, match="park"):
        _FakeTransaction(store._database).execute_update(
            "UPDATE tr_settle_outbox SET status='pending', last_error=@err, "
            "next_attempt_at=@next_at, lease_owner=NULL, leased_until=NULL, "
            "updated_at=@now WHERE authorization_id=@aid AND intent_kind=@kind "
            "AND status='pending'",
            params={
                "attempts": 0,
                "err": "typed store unavailable",
                "next_at": NOW,
                "now": NOW,
                "aid": auth.id,
                "kind": "settle",
            },
        )
    with pytest.raises(AssertionError, match="reservation-claim"):
        _FakeTransaction(store._database).execute_update(
            "UPDATE tr_reservation SET settled=true, actual_micro=@actual, "
            "settled_usage_type=@sut WHERE reservation_id=@rid",
            params={"rid": "missing", "actual": 0, "sut": "Credits"},
        )
    with pytest.raises(AssertionError, match="credit-release"):
        _FakeTransaction(store._database).execute_update(
            "UPDATE tr_credit_balance SET reserved = reserved - @hold, "
            "total_usage = total_usage + @actual WHERE workspace_id=@ws AND shard=@shard",
            params={"hold": 1, "actual": 1, "ws": "ws", "shard": 0},
        )
    with pytest.raises(AssertionError, match="key-release"):
        _FakeTransaction(store._database).execute_update(
            "UPDATE tr_key_limit SET reserved = reserved - @hold, usage = usage + @actual "
            "WHERE key_hash=@kh AND shard=@shard",
            params={
                "hold": 1,
                "actual": 1,
                "kh": "key",
                "shard": 0,
                "day_floor": NOW,
                "week_floor": NOW,
                "month_floor": NOW,
            },
        )


# Functional (settle route)


def test_flag_on_successful_typed_settle_enqueues_frozen_done_row(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, _db, _bt = fake_store
    ws = "ws-route-settle"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    settings = Settings(environment="test", settle_outbox_enabled=True)
    client = _client(settings)
    body = _settle_json(auth.id)

    resp = client.post("/v1/internal/gateway/settle", json=body)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    row = _outbox(store).get(auth.id, "settle")
    assert row is not None
    assert row.status == "done"
    assert row.actual_cost_micro == data["cost_microdollars"]
    assert row.settle_origin == "typed"
    assert row.intent_kind == "settle"
    assert row.selected_endpoint_id == ENDPOINT_ID
    assert row.model_id == MODEL_ID
    assert row.selected_usage_type == "Credits"
    assert row.settle_body is None
    assert row.terminal_at is not None


def test_internal_settle_enqueues_auto_refill_without_charging_in_process(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _bt = fake_store
    ws = "ws-internal-auto-refill"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    charged: list[str] = []
    monkeypatch.setattr(
        "trusted_router.services.auto_refill.maybe_charge_after_settle",
        lambda workspace_id, **_kwargs: charged.append(workspace_id),
    )
    client = _client(
        Settings(
            environment="test",
            service_surface="internal",
            settle_outbox_enabled=True,
        )
    )

    response = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert response.status_code == 200, response.text
    queued = _outbox(store).get_auto_refill(auth.id)
    assert queued is not None
    assert queued.workspace_id == ws
    assert queued.status == "pending"
    assert charged == []


def test_combined_settle_preserves_in_process_auto_refill(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _bt = fake_store
    ws = "ws-combined-auto-refill"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    charged: list[str] = []
    monkeypatch.setattr(
        "trusted_router.services.auto_refill.maybe_charge_after_settle",
        lambda workspace_id, **_kwargs: charged.append(workspace_id),
    )
    client = _client(
        Settings(
            environment="test",
            service_surface="combined",
            settle_outbox_enabled=True,
        )
    )

    response = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert response.status_code == 200, response.text
    assert charged == [ws]
    assert _outbox(store).get_auto_refill(auth.id) is None


def test_inline_failure_after_stripe_then_cross_minute_drain_creates_one_payment_intent(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One settlement has one Stripe identity across combined and control paths."""
    store, db, _bt = fake_store
    ws = "ws-auto-refill-cross-surface"
    _seed_credit(store, ws, total=1_000_000)
    store.update_auto_refill_settings(
        ws,
        enabled=True,
        threshold_microdollars=5_000_000,
        amount_microdollars=20_000_000,
    )
    store.set_stripe_customer(
        ws,
        customer_id="cus_cross_surface",
        payment_method_id="pm_cross_surface",
    )
    key = _make_key(store, ws, limit=None)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth)
    row.auto_refill_workspace_id = ws
    outbox = _outbox(store)
    assert outbox.enqueue(row) == "inserted"

    class CrossingMinute(dt.datetime):
        current = dt.datetime(2030, 1, 1, 12, 0, 59, tzinfo=dt.UTC)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return cls.current if tz is not None else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(auto_refill_mod, "datetime", CrossingMinute)
    submitted_keys: list[str] = []
    created_by_key: dict[str, Any] = {}

    def create_payment_intent(**kwargs: Any) -> Any:
        idem = str(kwargs["idempotency_key"])
        submitted_keys.append(idem)
        intent = created_by_key.setdefault(idem, type("Intent", (), {"id": "pi_once"})())
        if len(submitted_keys) == 1:
            raise RuntimeError("response lost after Stripe accepted the PaymentIntent")
        return intent

    monkeypatch.setattr("stripe.PaymentIntent.create", create_payment_intent)
    combined = _client(
        Settings(
            environment="test",
            service_surface="combined",
            settle_outbox_enabled=True,
            stripe_secret_key="sk_test_combined",  # noqa: S106 - fixture credential.
        )
    )

    response = combined.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert response.status_code == 200, response.text
    CrossingMinute.current = dt.datetime(2030, 1, 1, 12, 6, tzinfo=dt.UTC)
    db.settle_outbox[(auth.id, "settle")]["auto_refill_next_attempt_at"] = EXPIRED_AT
    drained = auto_refill_drain_mod.drain_auto_refill_outbox(
        Settings(
            environment="test",
            service_surface="control",
            stripe_secret_key="sk_test_control",  # noqa: S106 - fixture credential.
        ),
        limit=10,
    )

    assert drained["claimed"] == 1
    assert submitted_keys == [
        f"auto-refill-settlement:{auth.id}",
        f"auto-refill-settlement:{auth.id}",
    ]
    assert len(created_by_key) == 1


@pytest.mark.parametrize("existing_status", ("leased", "dead"))
def test_internal_settle_attaches_refill_to_pre_cutover_row_before_finalize(
    fake_store: tuple[Any, Any, Any],
    existing_status: str,
) -> None:
    """A pre-cutover settlement row cannot silently lose its refill sub-work."""
    store, db, _bt = fake_store
    ws = f"ws-pre-cutover-{existing_status}"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    outbox = _outbox(store)
    assert outbox.enqueue(_row(auth, origin="typed")) == "inserted"
    if existing_status == "leased":
        [claimed] = outbox.claim(lease_seconds=300)
        assert claimed.authorization_id == auth.id
    else:
        db.settle_outbox[(auth.id, "settle")].update(
            status="dead",
            next_attempt_at=None,
        )
    client = _client(
        Settings(
            environment="test",
            service_surface="internal",
            settle_outbox_enabled=True,
        )
    )

    response = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert response.status_code == 200, response.text
    settled = store.get_gateway_authorization(auth.id)
    assert settled is not None and settled.settled is True
    refill = outbox.get_auto_refill(auth.id)
    assert refill is not None
    assert refill.workspace_id == ws
    assert refill.status == "pending"


def test_auto_refill_drain_is_idempotent_after_duplicate_enqueue(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-auto-refill-duplicate"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth)
    outbox = _outbox(store)
    assert outbox.enqueue(row) == "inserted"
    row.auto_refill_workspace_id = ws
    assert outbox.enqueue(row) == "refreshed"
    db.settle_outbox[(auth.id, "settle")]["auto_refill_next_attempt_at"] = EXPIRED_AT
    settled_auth = GatewayAuthorization(**{**auth.__dict__, "settled": True})
    monkeypatch.setattr(
        type(store),
        "get_gateway_authorization",
        lambda _self, _authorization_id: settled_auth,
    )
    calls: list[tuple[str, str | None]] = []

    def charge(
        workspace_id: str,
        *,
        settings: Settings,
        idempotency_key: str | None = None,
    ) -> AutoRefillOutcome:
        _ = settings
        calls.append((workspace_id, idempotency_key))
        return AutoRefillOutcome(fired=True, reason="charged", payment_intent_id="pi_once")

    monkeypatch.setattr(auto_refill_drain_mod, "maybe_charge_after_settle", charge)
    settings = Settings(
        environment="test",
        service_surface="control",
        stripe_secret_key="sk_test_control",  # noqa: S106 - fixture credential.
    )

    first = auto_refill_drain_mod.drain_auto_refill_outbox(settings, limit=10)
    second = auto_refill_drain_mod.drain_auto_refill_outbox(settings, limit=10)

    assert first["claimed"] == 1
    assert second["claimed"] == 0
    assert calls == [(ws, f"auto-refill-settlement:{auth.id}")]
    queued = outbox.get_auto_refill(auth.id)
    assert queued is not None and queued.status == "done"


def test_stale_auto_refill_queue_emits_page_worthy_signal(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-stale-auto-refill")
    row = _row(auth)
    row.auto_refill_workspace_id = auth.workspace_id
    outbox = _outbox(store)
    outbox.enqueue(row)
    db.settle_outbox[(auth.id, "settle")]["auto_refill_enqueued_at"] = EXPIRED_AT
    alerts: list[str] = []
    monkeypatch.setattr(
        auto_refill_drain_mod,
        "ops_alert",
        lambda message, **_kwargs: alerts.append(message),
    )

    result = auto_refill_drain_mod.drain_auto_refill_outbox(
        Settings(environment="test", service_surface="control"),
        limit=10,
    )

    assert result["oldest_age_seconds"] is not None
    assert result["oldest_age_seconds"] >= auto_refill_drain_mod.STALE_AFTER_SECONDS
    assert any("ALERT auto-refill outbox stale" in message for message in alerts)


def test_auto_refill_freshness_reads_only_the_sparse_pending_index(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, _db, _bt = fake_store

    assert _outbox(store).auto_refill_pending_freshness() == (None, 0)


def test_auto_refill_pass_debounces_transient_store_errors_until_sustained(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0

    def transient_then_recover(
        _settings: Settings,
        *,
        limit: int,
    ) -> dict[str, Any]:
        nonlocal attempts
        assert limit == 10
        attempts += 1
        if attempts <= 4:
            raise DeadlineExceeded("temporary Spanner deadline")
        return {"claimed": 0}

    monkeypatch.setattr(
        auto_refill_drain_mod,
        "drain_auto_refill_outbox",
        transient_then_recover,
    )
    drain_pass = AutoRefillDrainPass()
    settings = Settings(environment="test", service_surface="control")

    with caplog.at_level(logging.INFO, logger=auto_refill_drain_mod.__name__):
        assert drain_pass.run(settings, limit=10) is None
        assert drain_pass.run(settings, limit=10) is None
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

        assert drain_pass.run(settings, limit=10) is None
        alerts = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert len(alerts) == 1
        assert "consecutive_failures=3" in alerts[0].getMessage()

        assert drain_pass.run(settings, limit=10) is None
        assert len([record for record in caplog.records if record.levelno >= logging.ERROR]) == 1

        assert drain_pass.run(settings, limit=10) == {"claimed": 0}

    assert drain_pass.consecutive_transient_failures == 0
    assert any(
        "recovered after 4 transient failures" in record.getMessage() for record in caplog.records
    )


def test_auto_refill_pass_does_not_hide_application_bugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_settings: Settings, *, limit: int) -> dict[str, Any]:
        _ = limit
        raise ValueError("invalid queue row")

    monkeypatch.setattr(auto_refill_drain_mod, "drain_auto_refill_outbox", broken)

    with pytest.raises(ValueError, match="invalid queue row"):
        AutoRefillDrainPass().run(
            Settings(environment="test", service_surface="control"),
            limit=10,
        )


def test_settle_emits_timing_line(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _db, _bt = fake_store
    ws = "ws-route-timing"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    [record] = _settle_timing_records(caplog)
    message = record.getMessage()
    for field in TIMING_FIELDS:
        assert re.search(rf"\b{field}=\d+\.\d\b", message)
    assert isinstance(record.args, tuple)
    assert record.args[0] == auth.id
    assert record.args[2] == "typed"
    total_ms_arg = record.args[3]
    assert isinstance(total_ms_arg, float)
    assert total_ms_arg > 0


def test_settle_replay_emits_no_timing_line(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _db, _bt = fake_store
    ws = "ws-route-timing-replay"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=True))
    first = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))
    assert first.status_code == 200, first.text
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        replay = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["already_settled"] is True
    assert _settle_timing_records(caplog) == []


def test_flag_on_refund_enqueues_refund_done_row(fake_store: tuple[Any, Any, Any]) -> None:
    store, _db, _bt = fake_store
    ws = "ws-route-refund"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    resp = client.post("/v1/internal/gateway/refund", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    row = _outbox(store).get(auth.id, "refund")
    assert row is not None
    assert row.status == "done"
    assert row.intent_kind == "refund"
    assert row.settle_origin == "typed"


def test_flag_off_settle_creates_no_outbox_row(fake_store: tuple[Any, Any, Any]) -> None:
    store, _db, _bt = fake_store
    ws = "ws-route-off"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=False))

    resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    assert _outbox(store).get(auth.id, "settle") is None


def test_inline_finalize_false_leaves_outbox_pending(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws-route-free-first"
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
    )
    assert freed["outcome"] == SettleOutcome.SETTLED
    client = _client(
        Settings(environment="test", settle_outbox_enabled=True),
        raise_server_exceptions=False,
    )

    resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["already_settled"] is True
    row = _outbox(store).get(auth.id, "settle")
    assert row is not None and row.status == "pending"
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == 0


def test_enqueue_failure_does_not_fail_settle(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-route-enqueue-fails"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    def fail_enqueue(
        self: SpannerSettleOutbox,
        row: SettleOutboxRow,
        *,
        initial_delay_seconds: int = 0,
    ) -> str:
        raise RuntimeError("insert unavailable")

    monkeypatch.setattr(SpannerSettleOutbox, "enqueue", fail_enqueue)
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    assert _typed_credit(db, ws)["total_usage"] == resp.json()["data"]["cost_microdollars"]
    assert db.reservations[auth.credit_reservation_id]["settled"] is True


def test_broadcast_enqueue_failure_after_commit_does_not_fail_or_double_charge(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-route-broadcast-enqueue-fails"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    def fail_list_destinations(_workspace_id: str) -> list[Any]:
        raise DeadlineExceeded("broadcast store deadline exhausted")

    monkeypatch.setattr(store, "list_broadcast_destinations", fail_list_destinations)
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    with caplog.at_level(logging.ERROR, logger=GATEWAY_LOGGER):
        first = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert first.status_code == 200, first.text
    charged = first.json()["data"]["cost_microdollars"]
    assert _typed_credit(db, ws)["total_usage"] == charged
    assert db.reservations[auth.credit_reservation_id]["settled"] is True
    assert any(
        "broadcast_metadata_enqueue_failed" in record.getMessage()
        and auth.workspace_id in record.getMessage()
        for record in caplog.records
    )

    replay = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["already_settled"] is True
    assert _typed_credit(db, ws)["total_usage"] == charged


# Integration (drain + reaper)


def test_activity_pending_first_observation_parks_and_stamps_note(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-fresh")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.ACTIVITY_PENDING,
    )

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 1
    assert result["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    pending = ob.get(auth.id, "settle")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.attempts == 0
    assert pending.lease_owner is None
    assert pending.last_error is not None
    note_prefix = f"{drain_mod._ACTIVITY_PARK_NOTE} since="
    assert pending.last_error.startswith(note_prefix)
    since = dt.datetime.fromisoformat(
        pending.last_error.removeprefix(note_prefix).replace("Z", "+00:00")
    )
    assert since.tzinfo is not None and since.utcoffset() is not None


def test_activity_pending_second_cycle_preserves_original_since(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-two-cycles")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.ACTIVITY_PENDING,
    )

    first = drain_mod.drain_settle_outbox(10)
    first_row = ob.get(auth.id, "settle")
    assert first["claimed"] == 1
    assert first_row is not None and first_row.last_error is not None
    first_since = first_row.last_error.removeprefix(f"{drain_mod._ACTIVITY_PARK_NOTE} since=")
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    second = drain_mod.drain_settle_outbox(10)

    second_row = ob.get(auth.id, "settle")
    assert second["claimed"] == 1
    assert second_row is not None and second_row.last_error is not None
    second_since = second_row.last_error.removeprefix(f"{drain_mod._ACTIVITY_PARK_NOTE} since=")
    assert second_since == first_since
    assert second_row.attempts == 0


def test_typed_park_preserves_expired_activity_window(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-typed-outage")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    since = dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=drain_mod._ACTIVITY_REPAIR_MAX_AGE_SECONDS + 1
    )
    activity_note = (
        f"{drain_mod._ACTIVITY_PARK_NOTE} since={since.isoformat().replace('+00:00', 'Z')}"
    )
    db.settle_outbox[(auth.id, "settle")]["last_error"] = activity_note
    outcomes = iter(
        [
            ApplyOutcome.PARK_TYPED_UNAVAILABLE,
            ApplyOutcome.ACTIVITY_PENDING,
        ]
    )
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: next(outcomes),
    )

    first = drain_mod.drain_settle_outbox(10)

    assert first["outcomes"] == {ApplyOutcome.PARK_TYPED_UNAVAILABLE: 1}
    pending = ob.get(auth.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.attempts == 0
    assert pending.last_error == activity_note
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    second = drain_mod.drain_settle_outbox(10)

    assert second["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    dead = ob.get(auth.id, "settle")
    assert dead is not None and dead.status == "dead"
    assert dead.last_error == "activity_repair_expired"
    assert dead.settle_body is not None


def test_typed_park_without_activity_stamp_uses_plain_note(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _bt = fake_store
    auth = _bare_authorization("gwa-typed-outage-plain-note")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.PARK_TYPED_UNAVAILABLE,
    )

    result = drain_mod.drain_settle_outbox(10)

    assert result["outcomes"] == {ApplyOutcome.PARK_TYPED_UNAVAILABLE: 1}
    pending = ob.get(auth.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.attempts == 0
    assert pending.last_error == "typed store unavailable"
    assert "since=" not in pending.last_error


def test_inline_zero_cost_activity_failure_keeps_payload_without_park_note(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-activity-inline-zero-fails"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    monkeypatch.setattr(
        store.generation_store,
        "index_after_commit",
        lambda generation: False,
    )
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    response = client.post(
        "/v1/internal/gateway/settle",
        json=_zero_cost_settle_json(auth.id),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == 0
    ob = _outbox(store)
    pending = ob.get(auth.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.last_error is None
    assert pending.settle_body is not None
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    result = drain_mod.drain_settle_outbox(10)

    assert result["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    pending = ob.get(auth.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.settle_body is not None


def test_inline_zero_cost_without_park_note_resolves_after_index_succeeds(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, bt = fake_store
    ws = "ws-activity-inline-zero-repaired"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    original_index = store.generation_store.index_after_commit
    index_attempts: list[str] = []

    def fail_inline_only(generation: Any) -> bool:
        index_attempts.append(generation.id)
        if len(index_attempts) == 1:
            return False
        return original_index(generation)

    monkeypatch.setattr(
        store.generation_store,
        "index_after_commit",
        fail_inline_only,
    )
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    response = client.post(
        "/v1/internal/gateway/settle",
        json=_zero_cost_settle_json(auth.id),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == 0
    ob = _outbox(store)
    pending = ob.get(auth.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.last_error is None
    assert bt.committed == []
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    result = drain_mod.drain_settle_outbox(10)

    assert result["outcomes"] == {ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE: 1}
    completed = ob.get(auth.id, "settle")
    assert completed is not None and completed.status == "done"
    assert len(index_attempts) == 2
    assert bt.committed


def test_zero_cost_activity_pending_keeps_retrying_and_preserves_payload(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-activity-zero-retry"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    ob = _outbox(store)
    ob.enqueue(_row(auth, cost=0))
    monkeypatch.setattr(
        apply_mod,
        "_index_generation_after_commit",
        lambda typed_store, generation: False,
    )

    first = drain_mod.drain_settle_outbox(10)
    first_row = ob.get(auth.id, "settle")
    assert first["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    assert first_row is not None and first_row.status == "pending"
    assert first_row.settle_body is not None
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    second = drain_mod.drain_settle_outbox(10)

    second_row = ob.get(auth.id, "settle")
    assert second["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    assert second_row is not None and second_row.status == "pending"
    assert second_row.settle_body is not None


def test_zero_cost_activity_pending_resolves_after_index_succeeds(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, bt = fake_store
    ws = "ws-activity-zero-repaired"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    ob = _outbox(store)
    ob.enqueue(_row(auth, cost=0))
    original_index = store.generation_store.index_after_commit
    index_attempts: list[str] = []

    def fail_once(generation: Any) -> bool:
        index_attempts.append(generation.id)
        if len(index_attempts) == 1:
            return False
        return original_index(generation)

    monkeypatch.setattr(store.generation_store, "index_after_commit", fail_once)

    first = drain_mod.drain_settle_outbox(10)
    assert first["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    assert bt.committed == []
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    second = drain_mod.drain_settle_outbox(10)

    completed = ob.get(auth.id, "settle")
    assert second["outcomes"] == {ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE: 1}
    assert completed is not None and completed.status == "done"
    assert len(index_attempts) == 2
    assert bt.committed


def test_activity_pending_old_row_with_new_window_still_parks(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-old-row-new-window")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    created_at = dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=drain_mod._ACTIVITY_REPAIR_MAX_AGE_SECONDS * 2
    )
    db.settle_outbox[(auth.id, "settle")]["created_at"] = created_at.isoformat().replace(
        "+00:00", "Z"
    )
    db.settle_outbox[(auth.id, "settle")]["last_error"] = "unrelated failure"
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.ACTIVITY_PENDING,
    )

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 1
    pending = ob.get(auth.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.attempts == 0
    assert pending.last_error is not None
    assert pending.last_error.startswith(f"{drain_mod._ACTIVITY_PARK_NOTE} since=")


def test_activity_pending_over_window_marks_dead_preserves_payload_and_alerts(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-expired")
    ob = _outbox(store)
    ob.enqueue(
        _row(
            auth,
            settle_body=json.dumps(_settle_json(auth.id, request_id="req-activity-expired")),
        )
    )
    since = dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=drain_mod._ACTIVITY_REPAIR_MAX_AGE_SECONDS + 1
    )
    db.settle_outbox[(auth.id, "settle")]["last_error"] = (
        f"{drain_mod._ACTIVITY_PARK_NOTE} since={since.isoformat().replace('+00:00', 'Z')}"
    )
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.ACTIVITY_PENDING,
    )

    with caplog.at_level(logging.ERROR, logger=drain_mod.__name__):
        result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 1
    assert result["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    completed = ob.get(auth.id, "settle")
    assert completed is not None and completed.status == "dead"
    assert completed.settle_body is not None
    assert completed.last_error == "activity_repair_expired"
    alerts = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.ERROR and "ALERT" in record.getMessage()
    ]
    assert len(alerts) == 1
    assert f"authorization_id={auth.id}" in alerts[0]
    assert f"generation_id={drain_mod.generation_id_for_authorization(auth.id)}" in alerts[0]
    assert "request_id=req-activity-expired" in alerts[0]
    assert f"reservation_id={auth.credit_reservation_id}" in alerts[0]
    assert "CHARGE IS ALREADY APPLIED" in alerts[0]
    assert "Spanner is correct" in alerts[0]
    assert "only the per-request Bigtable activity row is missing" in alerts[0]
    assert "row is now dead" in alerts[0]
    assert "settle_body PRESERVED" in alerts[0]
    assert "set the row back to pending to let the drain retry" in alerts[0]
    assert "reconcile/generation-activity" not in alerts[0]


def test_activity_pending_lost_lease_skips_false_alert(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-lost-lease")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    since = dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=drain_mod._ACTIVITY_REPAIR_MAX_AGE_SECONDS + 1
    )
    db.settle_outbox[(auth.id, "settle")]["last_error"] = (
        f"{drain_mod._ACTIVITY_PARK_NOTE} since={since.isoformat().replace('+00:00', 'Z')}"
    )
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.ACTIVITY_PENDING,
    )
    monkeypatch.setattr(SpannerSettleOutbox, "mark", lambda *args, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger=drain_mod.__name__):
        result = drain_mod.drain_settle_outbox(10)

    assert result["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    assert not [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "ALERT" in record.getMessage()
    ]
    assert any(
        record.levelno == logging.WARNING
        and "escalation skipped" in record.getMessage()
        and f"authorization_id={auth.id}" in record.getMessage()
        and "intent_kind=settle" in record.getMessage()
        for record in caplog.records
    )


def test_lost_lease_dead_letter_alerts_only_when_fence_wins(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    ob = _outbox(store)
    stale_auth = _bare_authorization("gwa-missing-stale-owner")
    winner_auth = _bare_authorization("gwa-missing-winning-owner")
    ob.enqueue(_row(stale_auth))
    ob.enqueue(_row(winner_auth))
    claimed = {row.authorization_id: row for row in ob.claim(limit=2, lease_seconds=300)}
    stale = claimed[stale_auth.id]
    winner = claimed[winner_auth.id]
    stale_record = db.settle_outbox[(stale_auth.id, "settle")]
    stale_record["lease_owner"] = None
    stale_record["leased_until"] = None

    with caplog.at_level(logging.WARNING, logger=drain_mod.__name__):
        drain_mod._resolve_row(
            ob,
            stale,
            ApplyOutcome.RESERVATION_MISSING,
            error_note=None,
        )
        drain_mod._resolve_row(
            ob,
            winner,
            ApplyOutcome.RESERVATION_MISSING,
            error_note=None,
        )

    stale_row = ob.get(stale_auth.id, "settle")
    winner_row = ob.get(winner_auth.id, "settle")
    assert stale_row is not None and stale_row.status == "pending"
    assert stale_row.last_error is None
    assert winner_row is not None and winner_row.status == "dead"
    alerts = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "ALERT" in record.getMessage()
    ]
    assert len(alerts) == 1
    assert f"authorization_id={winner_auth.id}" in alerts[0].getMessage()
    assert not any(
        record.levelno == logging.ERROR
        and f"authorization_id={stale_auth.id}" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        record.levelno == logging.WARNING
        and "resolution skipped" in record.getMessage()
        and f"authorization_id={stale_auth.id}" in record.getMessage()
        and "intent_kind=settle" in record.getMessage()
        for record in caplog.records
    )


def test_activity_pending_escalation_keeps_retention_pinned(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    ws = "ws-activity-retention"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth)
    ob = _outbox(store)
    ob.enqueue(row)
    monkeypatch.setattr(
        apply_mod,
        "_index_generation_after_commit",
        lambda typed_store, generation: False,
    )
    assert apply_mod.apply_frozen_settle(row) == ApplyOutcome.ACTIVITY_PENDING
    assert db.reservations[auth.credit_reservation_id]["settled"] is True
    assert db.gateway_authorizations[auth.id]["settled"] is True
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is None
    assert db.gateway_authorizations[auth.id]["terminal_at"] is None
    since = dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=drain_mod._ACTIVITY_REPAIR_MAX_AGE_SECONDS + 1
    )
    db.settle_outbox[(auth.id, "settle")]["last_error"] = (
        f"{drain_mod._ACTIVITY_PARK_NOTE} since={since.isoformat().replace('+00:00', 'Z')}"
    )

    result = drain_mod.drain_settle_outbox(10)

    assert result["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    assert ob.get(auth.id, "settle").status == "dead"
    # Deliberate: these records are the evidence needed to repair the activity.
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is None
    assert db.gateway_authorizations[auth.id]["terminal_at"] is None


def test_activity_pending_dead_row_reset_to_pending_is_reclaimed(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-retry-dead")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    ob.mark(
        auth.id,
        "settle",
        done=False,
        error="activity_repair_expired",
        force_dead=True,
    )
    db.settle_outbox[(auth.id, "settle")]["status"] = "pending"
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"
    applied: list[str] = []

    def apply_repaired(row: SettleOutboxRow) -> str:
        applied.append(row.authorization_id)
        return ApplyOutcome.SETTLED_NOW

    monkeypatch.setattr(drain_mod, "apply_frozen_settle", apply_repaired)

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 1
    assert result["outcomes"] == {ApplyOutcome.SETTLED_NOW: 1}
    assert applied == [auth.id]
    assert ob.get(auth.id, "settle").status == "done"


@pytest.mark.parametrize(
    "since",
    [
        "not-an-iso-timestamp",
        (
            dt.datetime.now(dt.UTC)
            - dt.timedelta(seconds=drain_mod._ACTIVITY_REPAIR_MAX_AGE_SECONDS + 1)
        )
        .replace(tzinfo=None)
        .isoformat(),
    ],
    ids=["malformed", "naive"],
)
def test_activity_pending_malformed_or_naive_since_still_parks(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    since: str,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-malformed")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    db.settle_outbox[(auth.id, "settle")]["last_error"] = (
        f"{drain_mod._ACTIVITY_PARK_NOTE} since={since}"
    )
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.ACTIVITY_PENDING,
    )

    with caplog.at_level(logging.WARNING, logger=drain_mod.__name__):
        result = drain_mod.drain_settle_outbox(10)

    assert result["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    pending = ob.get(auth.id, "settle")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.attempts == 0
    assert pending.last_error is not None
    stamped_since = dt.datetime.fromisoformat(
        pending.last_error.removeprefix(f"{drain_mod._ACTIVITY_PARK_NOTE} since=").replace(
            "Z", "+00:00"
        )
    )
    assert stamped_since.tzinfo is not None
    assert any(
        f"authorization_id={auth.id}" in record.getMessage()
        and "malformed since" in record.getMessage()
        for record in caplog.records
    )
    assert not [record for record in caplog.records if record.levelno == logging.ERROR]


def test_activity_pending_future_since_is_clamped(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    auth = _bare_authorization("gwa-activity-future")
    ob = _outbox(store)
    ob.enqueue(_row(auth))
    created_at = dt.datetime.now(dt.UTC) - dt.timedelta(
        seconds=drain_mod._ACTIVITY_REPAIR_MAX_AGE_SECONDS + 1
    )
    future_since = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    db.settle_outbox[(auth.id, "settle")]["created_at"] = created_at.isoformat().replace(
        "+00:00", "Z"
    )
    db.settle_outbox[(auth.id, "settle")]["last_error"] = (
        f"{drain_mod._ACTIVITY_PARK_NOTE} since={future_since.isoformat().replace('+00:00', 'Z')}"
    )
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.ACTIVITY_PENDING,
    )

    with caplog.at_level(logging.WARNING, logger=drain_mod.__name__):
        result = drain_mod.drain_settle_outbox(10)

    assert result["outcomes"] == {ApplyOutcome.ACTIVITY_PENDING: 1}
    pending = ob.get(auth.id, "settle")
    assert pending is not None and pending.status == "pending"
    assert pending.last_error is not None
    stamped_since = dt.datetime.fromisoformat(
        pending.last_error.removeprefix(f"{drain_mod._ACTIVITY_PARK_NOTE} since=").replace(
            "Z", "+00:00"
        )
    )
    assert stamped_since < future_since
    assert any(
        record.levelno == logging.WARNING
        and "future since" in record.getMessage()
        and f"authorization_id={auth.id}" in record.getMessage()
        and "intent_kind=settle" in record.getMessage()
        for record in caplog.records
    )


def test_drain_reaps_expired_unguarded_holds(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws-drain-reap-unguarded"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _expired_authorization(store, workspace_id=ws, key_hash=key.hash)
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 0
    assert result["reaped"] == 1
    assert _typed_credit(db, ws)["reserved"] == 0
    reservation = db.reservations[auth.credit_reservation_id]
    assert reservation["settled"] is True
    assert reservation["actual_micro"] == 0


def test_drain_reap_respects_outbox_guard(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-drain-reap-guard"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _expired_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth)
    ob = _outbox(store)
    ob.enqueue(row)
    original_typed_store = apply_mod.typed_billing_store
    monkeypatch.setattr(apply_mod, "typed_billing_store", lambda: None)

    parked = drain_mod.drain_settle_outbox(10)

    assert parked["claimed"] == 1
    assert parked["outcomes"] == {ApplyOutcome.PARK_TYPED_UNAVAILABLE: 1}
    assert parked["reaped"] == 0
    parked_row = ob.get(auth.id, "settle")
    assert parked_row is not None
    assert parked_row.status == "pending"
    assert parked_row.attempts == 0
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE
    assert db.reservations[auth.credit_reservation_id]["settled"] is False

    monkeypatch.setattr(apply_mod, "typed_billing_store", original_typed_store)
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"
    recovered = drain_mod.drain_settle_outbox(10)

    assert recovered["claimed"] == 1
    assert recovered["outcomes"] == {ApplyOutcome.SETTLED_NOW: 1}
    assert recovered["reaped"] == 0
    assert ob.get(auth.id, "settle").status == "done"
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == row.actual_cost_micro
    assert _typed_credit(db, ws)["reserved"] == 0
    assert _typed_credit(db, ws)["total_usage"] == row.actual_cost_micro

    again = drain_mod.drain_settle_outbox(10)
    assert again["claimed"] == 0
    assert again["reaped"] == 0
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == row.actual_cost_micro


def test_drain_reap_limit_respected(fake_store: tuple[Any, Any, Any]) -> None:
    store, db, _bt = fake_store
    ws = "ws-drain-reap-limit"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auths = [_expired_authorization(store, workspace_id=ws, key_hash=key.hash) for _ in range(3)]
    assert _typed_credit(db, ws)["reserved"] == ESTIMATE * 3

    result = drain_mod.drain_settle_outbox(10)

    assert result["reaped"] == 3
    assert _typed_credit(db, ws)["reserved"] == 0
    for auth in auths:
        reservation = db.reservations[auth.credit_reservation_id]
        assert reservation["settled"] is True
        assert reservation["actual_micro"] == 0

    again = drain_mod.drain_settle_outbox(10)
    assert again["reaped"] == 0


def test_zero_cost_settle_reaper_race_indexes_activity_and_resolves_done(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, bt = fake_store
    ws = "ws-drain-zero-reaper-race"
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
    )
    assert freed["outcome"] == SettleOutcome.SETTLED
    ob = _outbox(store)
    ob.enqueue(_row(auth, cost=0))
    credit_before = dict(_typed_credit(db, ws))
    key_before = dict(_typed_key(db, key.hash))
    assert _generation_count(db) == 0
    caplog.set_level(logging.WARNING)

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 1
    assert result["outcomes"] == {ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE: 1}
    assert result["recovered_micro"] == 0
    assert result["reaped"] == 0
    assert ob.get(auth.id, "settle").status == "done"
    assert _typed_credit(db, ws) == credit_before
    assert _typed_key(db, key.hash) == key_before
    assert _generation_count(db) == 0
    assert bt.committed
    messages = [rec.message for rec in caplog.records]
    assert any(
        "settle intent found reservation already zero-resolved" in msg
        and f"authorization_id={auth.id}" in msg
        and f"reservation_id={auth.credit_reservation_id}" in msg
        and "likely reaper race" in msg
        and "activity index verified without a Spanner billing write" in msg
        for msg in messages
    )
    assert not any("ALERT" in msg for msg in messages)


def test_nonzero_settle_reaper_race_still_alerts_lost_charge(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-drain-nonzero-reaper-race"
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
    )
    assert freed["outcome"] == SettleOutcome.SETTLED
    ob = _outbox(store)
    ob.enqueue(_row(auth, cost=777_777))
    credit_before = dict(_typed_credit(db, ws))
    key_before = dict(_typed_key(db, key.hash))
    caplog.set_level(logging.WARNING)

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 1
    assert result["outcomes"] == {ApplyOutcome.ALREADY_RELEASED_FREE: 1}
    assert result["recovered_micro"] == 0
    assert result["reaped"] == 0
    lost = ob.get(auth.id, "settle")
    assert lost is not None
    assert lost.status == "dead"
    assert lost.last_error == "already_released_free: settle charge was lost"
    assert _typed_credit(db, ws) == credit_before
    assert _typed_key(db, key.hash) == key_before
    assert any("ALERT settle outbox lost charge" in rec.message for rec in caplog.records)


def test_duplicate_zero_cost_settle_replay_resolves_done_with_warning(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, bt = fake_store
    ws = "ws-drain-zero-replay"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    row = _row(auth, cost=0)
    ob = _outbox(store)
    ob.enqueue(row)
    assert apply_mod.apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    credit_before = dict(_typed_credit(db, ws))
    key_before = dict(_typed_key(db, key.hash))
    generation_count_before = _generation_count(db)
    committed_before = list(bt.committed)
    activity_rows_before = set(bt.rows)
    assert generation_count_before == 1
    caplog.set_level(logging.WARNING)

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 1
    assert result["outcomes"] == {ApplyOutcome.RESOLVED_ZERO_COST_ELSEWHERE: 1}
    assert result["recovered_micro"] == 0
    assert result["reaped"] == 0
    assert ob.get(auth.id, "settle").status == "done"
    assert _typed_credit(db, ws) == credit_before
    assert _typed_key(db, key.hash) == key_before
    assert _generation_count(db) == generation_count_before
    assert len(bt.committed) > len(committed_before)
    assert set(bt.rows) == activity_rows_before
    messages = [rec.message for rec in caplog.records]
    assert any(
        "settle intent found reservation already zero-resolved" in msg
        and f"authorization_id={auth.id}" in msg
        and f"reservation_id={auth.credit_reservation_id}" in msg
        and "activity index verified without a Spanner billing write" in msg
        for msg in messages
    )
    assert not any("ALERT" in msg for msg in messages)


def test_lost_charge_recovery_end_to_end(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ws = "ws-drain-recover"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    original = store.typed_finalize_gateway_authorization_result
    state = {"crash": True}

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        if state["crash"]:
            state["crash"] = False
            raise RuntimeError("crash after enqueue")
        return original(*args, **kwargs)

    store.typed_finalize_gateway_authorization_result = crash_once
    client = _client(
        Settings(environment="test", settle_outbox_enabled=True),
        raise_server_exceptions=False,
    )
    first = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))
    assert first.status_code == 500
    row = _outbox(store).get(auth.id, "settle")
    assert row is not None and row.status == "pending"
    assert db.reservations[auth.credit_reservation_id]["settled"] is False
    db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    assert reap_expired_reservations(store._database, store._param_types, now=NOW) == 0
    assert db.reservations[auth.credit_reservation_id]["settled"] is False

    drained = client.post("/v1/internal/gateway/settle-outbox/drain?limit=10")

    assert drained.status_code == 200, drained.text
    payload = drained.json()
    assert payload["claimed"] == 1
    assert payload["outcomes"] == {ApplyOutcome.SETTLED_NOW: 1}
    assert payload["recovered_micro"] == row.actual_cost_micro
    assert payload["reaped"] == 0
    assert _outbox(store).get(auth.id, "settle").status == "done"
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == row.actual_cost_micro
    assert _typed_credit(db, ws)["total_usage"] == row.actual_cost_micro
    assert reap_expired_reservations(store._database, store._param_types, now=NOW) == 0


@pytest.mark.parametrize(
    "failure",
    [
        LeaseSettlementError("unknown regional reservation"),
        RegionalLeaseLedgerError("regional ledger unavailable"),
    ],
)
def test_regional_settlement_failure_is_retryable_after_outbox_enqueue(
    fake_store: tuple[Any, Any, Any],
    failure: Exception,
) -> None:
    store, db, _bt = fake_store
    ws = "ws-regional-settle-retry"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)

    def fail_finalize(*_args: Any, **_kwargs: Any) -> TypedFinalizeResult:
        raise failure

    store.typed_finalize_gateway_authorization_result = fail_finalize
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    response = client.post(
        "/v1/internal/gateway/settle",
        json=_settle_json(auth.id),
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["type"] == "service_unavailable"
    row = _outbox(store).get(auth.id, "settle")
    assert row is not None and row.status == "pending"
    assert db.reservations[auth.credit_reservation_id]["settled"] is False


def test_user_model_inline_finalize_loss_repairs_one_payout_from_frozen_outbox(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, _db, _bt = fake_store
    ws = "ws-user-model-outbox-repair"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    original = store.typed_finalize_gateway_authorization_result

    def lose_inline_finalize(*_args: Any, **_kwargs: Any) -> TypedFinalizeResult:
        return TypedFinalizeResult(finalized=False, activity_indexed=False)

    store.typed_finalize_gateway_authorization_result = lose_inline_finalize
    client = _client(Settings(environment="test", settle_outbox_enabled=True))
    response = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth.id,
            "actual_input_tokens": 1_000,
            "actual_output_tokens": 2_000,
            "request_id": "req-user-model-outbox-repair",
            "finish_reason": "stop",
            "status": "success",
            "elapsed_seconds": 0.2,
            "selected_model": USER_MODEL_ID,
            "selected_endpoint": USER_MODEL_ENDPOINT_ID,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["already_settled"] is True
    row = _outbox(store).get(auth.id, "settle")
    assert row is not None
    assert row.status == "pending"
    assert row.settle_body is not None

    store.typed_finalize_gateway_authorization_result = original
    assert apply_mod.apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_mod.apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    payout = 5_600
    assert store.earnings_summary("owner-outbox-repair")["total_earned"] == payout
    movements = store.list_credit_movements("user:owner-outbox-repair")
    assert len(movements) == 1
    assert movements[0].movement_id == user_model_payout_event_id(auth.id)
    assert movements[0].amount_microdollars == payout
    assert movements[0].custom_model_id == USER_MODEL_ID
    assert movements[0].counterparty_account_id == ws


def test_charged_settle_then_sibling_refund_resolves_and_arms_retention(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    ws = "ws-drain-sibling-refund"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    ob = _outbox(store)
    settle_row = _row(auth, cost=777_777)
    refund_row = _row(auth, intent="refund", cost=777_777)
    ob.enqueue(settle_row)
    ob.enqueue(refund_row, initial_delay_seconds=60)

    settled = drain_mod.drain_settle_outbox(10)

    assert settled["outcomes"] == {ApplyOutcome.SETTLED_NOW: 1}
    assert ob.get(auth.id, "settle").status == "done"
    assert ob.get(auth.id, "refund").status == "pending"
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is None
    assert db.gateway_authorizations[auth.id]["terminal_at"] is None
    db.settle_outbox[(auth.id, "refund")]["next_attempt_at"] = "2000-01-01T00:00:00Z"

    with caplog.at_level(logging.WARNING, logger=drain_mod.__name__):
        refunded = drain_mod.drain_settle_outbox(10)

    assert refunded["outcomes"] == {ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE: 1}
    completed_refund = ob.get(auth.id, "refund")
    assert completed_refund is not None and completed_refund.status == "done"
    assert any(
        "kept charge beat refund intent" in record.getMessage()
        and f"authorization_id={auth.id}" in record.getMessage()
        for record in caplog.records
    )
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is not None
    assert db.gateway_authorizations[auth.id]["terminal_at"] is not None
    # Money: the settle's charge stands, the refund replay moved no counters.
    credit = _typed_credit(db, ws)
    assert credit["reserved"] == 0
    assert credit["total_usage"] == 777_777
    key_row = _typed_key(db, key.hash)
    assert key_row["reserved"] == 0
    assert key_row["usage"] == 777_777


def test_charged_settle_with_no_generation_dead_letters_as_invalid_row(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = fake_store
    store.request_record_write_mode = "typed"
    ws = "ws-drain-malformed-settle"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    assert apply_mod.apply_frozen_settle(_row(auth, cost=777_777)) == ApplyOutcome.SETTLED_NOW
    ob = _outbox(store)
    ob.enqueue(_row(auth, cost=777_777))

    def fail_to_build_generation(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        apply_mod,
        "_frozen_generation",
        fail_to_build_generation,
    )

    drained = drain_mod.drain_settle_outbox(10)

    assert drained["outcomes"] == {ApplyOutcome.INVALID_ROW: 1}
    malformed = ob.get(auth.id, "settle")
    assert malformed is not None and malformed.status == "dead"
    assert malformed.last_error == "invalid_row"
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is None
    assert db.gateway_authorizations[auth.id]["terminal_at"] is None


def test_drain_leaves_terminal_rows_for_spanner_ttl(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, db, _bt = fake_store
    ob = _outbox(store)
    for aid in (
        "gwa-old-done-a",
        "gwa-old-done-b",
        "gwa-fresh-done",
        "gwa-pending",
        "gwa-dead",
        "gwa-release-approved",
    ):
        ob.enqueue(_row(_bare_authorization(aid)))

    old = "2000-01-01T00:00:00Z"
    ob.mark("gwa-old-done-a", "settle", done=True)
    ob.mark("gwa-old-done-b", "settle", done=True)
    ob.mark("gwa-fresh-done", "settle", done=True)
    ob.mark("gwa-dead", "settle", done=False, force_dead=True, error="manual review")
    db.settle_outbox[("gwa-release-approved", "settle")]["status"] = "release_approved"
    for aid in (
        "gwa-old-done-a",
        "gwa-old-done-b",
        "gwa-pending",
        "gwa-dead",
        "gwa-release-approved",
    ):
        db.settle_outbox[(aid, "settle")]["updated_at"] = old
    db.settle_outbox[("gwa-pending", "settle")]["next_attempt_at"] = "2999-01-01T00:00:00Z"

    result = drain_mod.drain_settle_outbox(10)

    assert result == {"claimed": 0, "outcomes": {}, "recovered_micro": 0, "purged": 0, "reaped": 0}
    assert ob.get("gwa-old-done-a", "settle").status == "done"
    assert ob.get("gwa-old-done-b", "settle").status == "done"
    assert ob.get("gwa-fresh-done", "settle").status == "done"
    assert ob.get("gwa-pending", "settle").status == "pending"
    assert ob.get("gwa-dead", "settle").status == "dead"
    assert ob.get("gwa-release-approved", "settle").status == "release_approved"


def test_drain_switch_coverage(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _db, _bt = fake_store
    ob = _outbox(store)
    ws = "ws-drain-switch"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    parked = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    error_auth = _legacy_authorization(store, workspace_id=ws, key_hash=key.hash)
    lost_auth = _legacy_authorization(store, workspace_id=ws, key_hash=key.hash)
    invalid_auth = _legacy_authorization(store, workspace_id=ws, key_hash=key.hash)
    ob.enqueue(_row(parked))
    ob.enqueue(_row(error_auth, origin="legacy"))
    ob.enqueue(_row(lost_auth, origin="legacy"))
    ob.enqueue(_row(invalid_auth, origin="legacy", selected_usage_type=None))
    monkeypatch.setattr(apply_mod, "typed_billing_store", lambda: None)
    sequence = iter(
        [
            ApplyOutcome.ERROR,
            ApplyOutcome.ALREADY_RELEASED_FREE,
            ApplyOutcome.INVALID_ROW,
        ]
    )
    real_apply = drain_mod.apply_frozen_settle

    def mixed_apply(row: SettleOutboxRow) -> str:
        if row.authorization_id == parked.id:
            return real_apply(row)
        return next(sequence)

    monkeypatch.setattr(drain_mod, "apply_frozen_settle", mixed_apply)
    caplog.set_level("WARNING")

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 4
    assert result["outcomes"] == {
        ApplyOutcome.PARK_TYPED_UNAVAILABLE: 1,
        ApplyOutcome.ERROR: 1,
        ApplyOutcome.ALREADY_RELEASED_FREE: 1,
        ApplyOutcome.INVALID_ROW: 1,
    }
    parked_row = ob.get(parked.id, "settle")
    assert parked_row is not None and parked_row.status == "pending"
    assert parked_row.attempts == 0
    assert parked_row.lease_owner is None
    error_row = ob.get(error_auth.id, "settle")
    assert error_row is not None and error_row.status == "pending" and error_row.attempts == 1
    lost_row = ob.get(lost_auth.id, "settle")
    assert lost_row is not None and lost_row.status == "dead"
    invalid_row = ob.get(invalid_auth.id, "settle")
    assert invalid_row is not None and invalid_row.status == "dead"
    assert any("ALERT settle outbox lost charge" in rec.message for rec in caplog.records)


def test_drain_resolves_recovery_outcomes_and_warning_gates(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _db, _bt = fake_store
    ob = _outbox(store)
    cases = {
        "gwa-refund-kept-charge": ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE,
        "gwa-refund-zero": ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE,
        "gwa-legacy-settle": ApplyOutcome.ALREADY_SETTLED_LEGACY,
        "gwa-legacy-refund-self": ApplyOutcome.ALREADY_SETTLED_LEGACY,
        "gwa-refund-already-free": ApplyOutcome.ALREADY_RELEASED_FREE,
        "gwa-missing": ApplyOutcome.RESERVATION_MISSING,
        "gwa-unknown": "mystery_outcome",
    }
    ob.enqueue(_row(_bare_authorization("gwa-refund-kept-charge"), intent="refund", cost=50))
    ob.enqueue(_row(_bare_authorization("gwa-refund-zero"), intent="refund", cost=0))
    ob.enqueue(_row(_bare_authorization("gwa-legacy-settle"), origin="legacy"))
    ob.enqueue(
        _row(_bare_authorization("gwa-legacy-settle"), intent="refund", origin="legacy"),
        initial_delay_seconds=60,
    )
    ob.enqueue(
        _row(_bare_authorization("gwa-legacy-refund-self"), intent="refund", origin="legacy")
    )
    ob.enqueue(_row(_bare_authorization("gwa-refund-already-free"), intent="refund"))
    ob.enqueue(_row(_bare_authorization("gwa-missing")))
    ob.enqueue(_row(_bare_authorization("gwa-unknown")))

    def fake_apply(row: SettleOutboxRow) -> str:
        return cases[row.authorization_id]

    monkeypatch.setattr(drain_mod, "apply_frozen_settle", fake_apply)
    caplog.set_level("WARNING")

    result = drain_mod.drain_settle_outbox(20)

    assert result["claimed"] == 7
    assert result["outcomes"] == {
        ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE: 2,
        ApplyOutcome.ALREADY_SETTLED_LEGACY: 2,
        ApplyOutcome.ALREADY_RELEASED_FREE: 1,
        ApplyOutcome.RESERVATION_MISSING: 1,
        "mystery_outcome": 1,
    }
    assert result["purged"] == 0
    assert ob.get("gwa-refund-kept-charge", "refund").status == "done"
    assert ob.get("gwa-refund-zero", "refund").status == "done"
    assert ob.get("gwa-legacy-settle", "settle").status == "done"
    assert ob.get("gwa-legacy-settle", "refund").status == "pending"
    assert ob.get("gwa-legacy-refund-self", "refund").status == "done"
    assert ob.get("gwa-refund-already-free", "refund").status == "done"
    assert ob.get("gwa-missing", "settle").status == "dead"
    unknown = ob.get("gwa-unknown", "settle")
    assert unknown is not None
    assert unknown.status == "pending"
    assert unknown.attempts == 1
    assert unknown.last_error == "unknown outcome: mystery_outcome"
    messages = [rec.message for rec in caplog.records]
    assert any(
        "kept charge beat refund intent authorization_id=gwa-refund-kept-charge" in msg
        for msg in messages
    )
    assert not any("gwa-refund-zero" in msg for msg in messages)
    assert any(
        "legacy settled with sibling refund intent authorization_id=gwa-legacy-settle" in msg
        for msg in messages
    )
    assert not any("gwa-legacy-refund-self" in msg for msg in messages)
    assert any(
        "ALERT settle outbox reservation missing authorization_id=gwa-missing" in msg
        for msg in messages
    )


def test_drain_resolve_errors_do_not_abort_later_rows(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _bt = fake_store
    ob = _outbox(store)
    first = _bare_authorization("gwa-resolve-error")
    second = _bare_authorization("gwa-resolve-ok")
    ob.enqueue(_row(first, cost=111))
    ob.enqueue(_row(second, cost=222))
    monkeypatch.setattr(
        drain_mod,
        "apply_frozen_settle",
        lambda row: ApplyOutcome.SETTLED_NOW,
    )
    original_mark = SpannerSettleOutbox.mark

    def flaky_mark(
        self: SpannerSettleOutbox,
        authorization_id: str,
        intent_kind: str,
        **kwargs: Any,
    ) -> str | None:
        if authorization_id == first.id:
            raise RuntimeError("mark unavailable")
        return original_mark(self, authorization_id, intent_kind, **kwargs)

    monkeypatch.setattr(SpannerSettleOutbox, "mark", flaky_mark)

    result = drain_mod.drain_settle_outbox(10)

    assert result["claimed"] == 2
    assert result["outcomes"] == {ApplyOutcome.SETTLED_NOW: 2, "resolve_error": 1}
    failed = ob.get(first.id, "settle")
    assert failed is not None
    assert failed.status == "pending"
    assert failed.lease_owner is not None
    assert ob.get(second.id, "settle").status == "done"


def test_drain_clamps_limit_to_500(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fake_store is not None

    class SpyOutbox:
        seen_limit: int | None = None

        def claim(self, *, limit: int) -> list[SettleOutboxRow]:
            self.seen_limit = limit
            return []

        def purge_done(self) -> int:
            return 0

    spy = SpyOutbox()
    monkeypatch.setattr(drain_mod, "spanner_settle_outbox", lambda: spy)

    result = drain_mod.drain_settle_outbox(99_999)

    assert spy.seen_limit == 500
    assert result == {"claimed": 0, "outcomes": {}, "recovered_micro": 0, "purged": 0, "reaped": 0}


def test_drain_endpoint_requires_internal_token(fake_store: tuple[Any, Any, Any]) -> None:
    _store, _db, _bt = fake_store
    token = "internal-test-token"  # noqa: S105 - test token.
    client = _client(Settings(environment="test", internal_gateway_token=token))

    missing = client.post("/v1/internal/gateway/settle-outbox/drain")
    wrong = client.post(
        "/v1/internal/gateway/settle-outbox/drain",
        headers={"x-trustedrouter-internal-token": "wrong"},
    )
    ok = client.post(
        "/v1/internal/gateway/settle-outbox/drain",
        headers={"x-trustedrouter-internal-token": token},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert ok.json() == {
        "claimed": 0,
        "outcomes": {},
        "recovered_micro": 0,
        "purged": 0,
        "reaped": 0,
    }


# --- Review-round regressions: the primary prod payout path -----------------


def _typed_settle_body(auth: GatewayAuthorization, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "authorization_id": auth.id,
        "actual_input_tokens": 1_000,
        "actual_output_tokens": 2_000,
        "request_id": "req-user-model-inline",
        "finish_reason": "stop",
        "status": "success",
        "elapsed_seconds": 0.2,
        "selected_model": USER_MODEL_ID,
        "selected_endpoint": USER_MODEL_ENDPOINT_ID,
    }
    body.update(overrides)
    return body


def test_user_model_inline_typed_finalize_pays_owner_once_and_replay_is_a_noop(
    fake_store: tuple[Any, Any, Any],
) -> None:
    """The path prod actually takes: inline typed finalize WINS on Spanner.

    The earlier test only covered the inline-LOSS → outbox repair path; a
    mutant dropping `user_model_payout` from the inline finalize passed the
    whole suite. Here the inline settle must pay exactly once, and both a
    duplicate HTTP settle and the outbox repair replay must leave the payout
    at one movement / one increment.
    """
    store, _db, _bt = fake_store
    ws = "ws-user-model-inline-win"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    response = client.post("/v1/internal/gateway/settle", json=_typed_settle_body(auth))
    assert response.status_code == 200, response.text
    assert response.json()["data"].get("already_settled") is not True
    payout = 5_600  # 70% of 8_000 µ$ (1k×2µ$ + 2k×3µ$)
    assert store.earnings_summary("owner-outbox-repair")["total_earned"] == payout
    movements = store.list_credit_movements("user:owner-outbox-repair")
    assert [m.movement_id for m in movements] == [user_model_payout_event_id(auth.id)]
    assert movements[0].created_at  # client timestamp landed (not a commit-ts write)
    # Payer ledger: exactly the owner-priced charge booked, hold fully released.
    payer = store._database.typed[CREDIT_BALANCE_TABLE][(ws, 0)]
    assert payer["total_usage"] == 8_000
    assert payer["reserved"] == 0

    replay = client.post("/v1/internal/gateway/settle", json=_typed_settle_body(auth))
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["already_settled"] is True
    # The inline win marks the outbox row done (terminal; frozen body dropped),
    # so the drain never re-applies it — the replay surface is the HTTP one.
    row = _outbox(store).get(auth.id, "settle")
    assert row is not None and row.status == "done"
    assert store.earnings_summary("owner-outbox-repair")["total_earned"] == payout
    assert len(store.list_credit_movements("user:owner-outbox-repair")) == 1


def test_user_model_payout_movement_pk_is_a_real_second_guard(
    fake_store: tuple[Any, Any, Any],
) -> None:
    """If the movement row already exists (a prior path paid this
    authorization), the finalize must NOT bump earnings again — the PK is
    the guard behind the claim, and it has to be load-bearing on its own."""
    store, _db, _bt = fake_store
    ws = "ws-user-model-second-guard"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    # Pre-pay through the other writer of the same movement id.
    assert store.credit_user_earnings(
        "owner-outbox-repair",
        5_600,
        user_model_payout_event_id(auth.id),
        custom_model_id=USER_MODEL_ID,
        payer_workspace_id=ws,
    )
    client = _client(Settings(environment="test", settle_outbox_enabled=False))
    response = client.post("/v1/internal/gateway/settle", json=_typed_settle_body(auth))
    assert response.status_code == 200, response.text
    assert store.earnings_summary("owner-outbox-repair")["total_earned"] == 5_600
    assert len(store.list_credit_movements("user:owner-outbox-repair")) == 1


def test_user_model_settle_does_not_double_bill_cached_prompt_tokens(
    fake_store: tuple[Any, Any, Any],
) -> None:
    """Owner endpoints speak the OpenAI dialect: prompt_tokens already
    includes the cached subset. Billing input + cache_read double-charged the
    prompt and let an owner reporting cached==prompt double their revenue."""
    store, _db, _bt = fake_store
    ws = "ws-user-model-cache"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=False))
    response = client.post(
        "/v1/internal/gateway/settle",
        json=_typed_settle_body(
            auth,
            actual_input_tokens=1_000,
            actual_output_tokens=10,
            cache_read_input_tokens=800,
        ),
    )
    assert response.status_code == 200, response.text
    # 1_000 prompt tokens at 2 µ$ + 10 completion at 3 µ$ = 2_030, not 3_630.
    assert response.json()["data"]["cost_microdollars"] == 2_030


def test_user_model_settle_is_capped_at_the_authorized_hold(
    fake_store: tuple[Any, Any, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The token counts are the payee's own meter; the caller's hold is the
    ceiling on both the charge and the 70% payout."""
    store, _db, _bt = fake_store
    ws = "ws-user-model-cap"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=False))
    with caplog.at_level(logging.WARNING):
        response = client.post(
            "/v1/internal/gateway/settle",
            json=_typed_settle_body(
                auth, actual_input_tokens=10_000_000, actual_output_tokens=10_000_000
            ),
        )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == ESTIMATE
    assert store.earnings_summary("owner-outbox-repair")["total_earned"] == ESTIMATE * 7 // 10
    assert any("user_model_settle_capped_to_hold" in r.getMessage() for r in caplog.records)


def test_user_model_settle_accepts_the_callers_raw_model_spelling(
    fake_store: tuple[Any, Any, Any],
) -> None:
    """The enclave may echo the caller's spelling as selected_model; a refused
    settle here strands the hold, so it must compare normalized."""
    store, _db, _bt = fake_store
    ws = "ws-user-model-raw-spelling"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=False))
    response = client.post(
        "/v1/internal/gateway/settle",
        json=_typed_settle_body(auth, selected_model="TR-USER-MODEL/OWNER-OUTBOX-REPAIR"),
    )
    assert response.status_code == 200, response.text


def test_user_model_settle_refuses_a_different_model_id(
    fake_store: tuple[Any, Any, Any],
) -> None:
    store, _db, _bt = fake_store
    ws = "ws-user-model-wrong-model"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_user_model_authorization(store, workspace_id=ws, key_hash=key.hash)
    client = _client(Settings(environment="test", settle_outbox_enabled=False))
    other = client.post(
        "/v1/internal/gateway/settle",
        json=_typed_settle_body(auth, selected_model="tr-user-model/owner-someone-else"),
    )
    assert other.status_code == 400, other.text


# ── Done-mark folded into the finalize commit ─────────────────────────────────
#
# Settle p90 was ~3.1s: three SERIAL multi-region Spanner commits (enqueue,
# finalize, mark) at 500-1000ms p90 each, attempts=1 always. The design doc's
# own §7 said to write status='done' in the finalize transaction once the
# outbox shared the instance. These pin that: one commit fewer, same lease
# fence, and the leased-row case still defers to the drain.


def _settle_with_sql_spy(
    store: Any, monkeypatch: pytest.MonkeyPatch, *, ws: str
) -> tuple[Any, list[tuple[int, str]], Any]:
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    calls: list[tuple[int, str]] = []
    original_update = _FakeTransaction.execute_update
    # A monotonic per-transaction number, NOT id(): CPython reuses the id of a
    # freed transaction object, so a later standalone-mark transaction could
    # alias the finalize one and pass a same-transaction check vacuously.
    next_txn = 0

    def spy(self: Any, sql: str, **kwargs: Any) -> int:
        nonlocal next_txn
        if "_spy_txn" not in self.__dict__:
            self.__dict__["_spy_txn"] = next_txn
            next_txn += 1
        calls.append((self.__dict__["_spy_txn"], sql))
        return original_update(self, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_update", spy)
    return auth, calls, _client(Settings(environment="test", settle_outbox_enabled=True))


def test_inline_settle_resolves_the_outbox_row_inside_the_finalize_commit(
    prod_shaped_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = prod_shaped_store
    auth, calls, client = _settle_with_sql_spy(store, monkeypatch, ws="ws-fold-done")

    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    row = db.settle_outbox[(auth.id, "settle")]
    assert row["status"] == "done"
    assert row["lease_owner"] is None
    assert row["terminal_at"] is not None
    # The retention contract rode along: no outstanding sibling intent, so the
    # shared records are armed for the 30-day policy in the SAME commit.
    assert db.gateway_authorizations[auth.id]["terminal_at"] is not None
    assert db.reservations[auth.credit_reservation_id]["terminal_at"] is not None

    marks = [
        txn for txn, sql in calls if sql.startswith("UPDATE tr_settle_outbox SET status=@status")
    ]
    assert len(marks) == 1, "the done-mark must run exactly once"
    [mark_txn] = marks
    finalize_tables = {
        txn for txn, sql in calls if "tr_reservation" in sql or "tr_credit_balance" in sql
    }
    assert mark_txn in finalize_tables, (
        "the done-mark ran in its own transaction, not the finalize's"
    )

    [record] = _settle_timing_records(caplog)
    assert isinstance(record.args, tuple)
    assert record.args[7] == 0.0  # mark_ms: no standalone mark commit


def test_inline_spend_lease_overrun_caps_charge_generation_typed_cost_and_outbox(
    prod_shaped_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, db, _bt = prod_shaped_store
    enqueued: list[SettleOutboxRow] = []
    real_enqueue = SpannerSettleOutbox.enqueue

    def capture(self: SpannerSettleOutbox, row: SettleOutboxRow, **kwargs: Any) -> str:
        enqueued.append(replace(row))
        return real_enqueue(self, row, **kwargs)

    monkeypatch.setattr(SpannerSettleOutbox, "enqueue", capture)
    ws = "ws-spend-lease-inline-clamp"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    allocation = 500
    markup_basis_points = 2_500
    app_owner = "owner-spend-lease-inline-clamp"
    auth = _typed_authorization(
        store,
        workspace_id=ws,
        key_hash=key.hash,
        app_id="app-spend-lease-inline-clamp",
        app_markup_basis_points=markup_basis_points,
        app_owner_user_id=app_owner,
    )
    _stamp_spend_lease_binding(db, auth, allocation_micro=allocation)
    settings = Settings(
        environment="test",
        settle_outbox_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_issuance_enabled=True,
        spend_lease_binding_enabled=True,
        spend_lease_pilot_workspace_ids=ws,
        spend_lease_signing_secret_name="test-secret",  # noqa: S106
    )

    with caplog.at_level(logging.WARNING):
        response = _client(settings).post(
            "/v1/internal/gateway/settle",
            json=_settle_json(auth.id),
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_microdollars"] == allocation
    assert _typed_credit(db, ws)["total_usage"] == allocation
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == allocation
    typed_auth = db.gateway_authorizations[auth.id]
    assert typed_auth["finalized_cost_microdollars"] == allocation
    outbox = db.settle_outbox[(auth.id, "settle")]
    assert outbox["actual_cost_micro"] == allocation
    markup = app_markup_microdollars_from_charge(allocation, markup_basis_points)
    payout = app_markup_owner_share_microdollars(markup)
    [frozen] = enqueued
    assert frozen.actual_cost_micro == allocation
    assert json.loads(frozen.settle_body or "{}")[APP_MARKUP_PAYOUT_SETTLE_FIELD] == payout
    [generation] = db.generation_records.values()
    generation_payload = json.loads(generation["payload"])
    assert generation_payload["total_cost_microdollars"] == allocation
    assert generation_payload["app_markup_microdollars"] == markup
    [analytics] = db.operational_analytics_outbox
    assert json.loads(analytics["payload"])["total_cost_microdollars"] == allocation
    assert store.earnings_summary(app_owner)["total_earned"] == payout

    replay = _client(settings).post(
        "/v1/internal/gateway/settle",
        json=_settle_json(auth.id),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["already_settled"] is True
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == allocation
    assert store.earnings_summary(app_owner)["total_earned"] == payout
    assert "billing.spend_lease_settle_capped_to_allocation" in caplog.text


def test_eager_mirror_runs_after_won_finalize_not_lost_replay(
    prod_shaped_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _bt = prod_shaped_store
    ws = "ws-spend-lease-eager-winner"
    _seed_credit(store, ws)
    key = _make_key(store, ws)
    winner = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    loser = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
    _stamp_spend_lease_binding(db, winner, allocation_micro=ESTIMATE)
    _stamp_spend_lease_binding(db, loser, allocation_micro=ESTIMATE)
    mirrored: list[str] = []
    monkeypatch.setattr(
        "trusted_router.routes.internal.gateway.mirror_finalized_spend_lease_best_effort",
        lambda _store, committed: mirrored.append(committed.id),
    )
    real_finalize = type(store).typed_finalize_gateway_authorization_result

    def finalize_with_lost_replay(self: Any, authorization_id: str, **kwargs: Any) -> Any:
        if authorization_id == loser.id:
            return TypedFinalizeResult(finalized=False, activity_indexed=False)
        return real_finalize(self, authorization_id, **kwargs)

    monkeypatch.setattr(
        type(store),
        "typed_finalize_gateway_authorization_result",
        finalize_with_lost_replay,
    )
    client = _client(Settings(environment="test", settle_outbox_enabled=True))

    won = client.post("/v1/internal/gateway/settle", json=_settle_json(winner.id))
    lost = client.post("/v1/internal/gateway/settle", json=_settle_json(loser.id))

    assert won.status_code == lost.status_code == 200
    assert mirrored == [winner.id]


def test_flag_off_settle_body_outbox_and_charge_ignore_lease_named_extras(
    prod_shaped_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _bt = prod_shaped_store
    captured: list[SettleOutboxRow] = []
    real_enqueue = SpannerSettleOutbox.enqueue

    def capture(self: SpannerSettleOutbox, row: SettleOutboxRow, **kwargs: Any) -> str:
        captured.append(row)
        return real_enqueue(self, row, **kwargs)

    monkeypatch.setattr(SpannerSettleOutbox, "enqueue", capture)
    client = _client(Settings(environment="test", settle_outbox_enabled=True))
    responses = []
    for suffix, extras in (
        ("plain", {}),
        (
            "lease-extras",
            {
                "spend_lease_id": "caller-forged",
                "spend_lease_gen": 99,
                "spend_lease_allocated_micro": 1,
                "settlement": "spend_lease",
            },
        ),
    ):
        ws = f"ws-flag-off-{suffix}"
        _seed_credit(store, ws)
        key = _make_key(store, ws)
        auth = _typed_authorization(store, workspace_id=ws, key_hash=key.hash)
        body = _settle_json(auth.id, request_id="same-request")
        body.update(extras)
        responses.append(client.post("/v1/internal/gateway/settle", json=body))

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["data"]["cost_microdollars"] == responses[1].json()["data"][
        "cost_microdollars"
    ]
    normalized_rows = []
    for row in captured:
        frozen = asdict(row)
        frozen["authorization_id"] = "normalized"
        frozen["reservation_id"] = "normalized"
        frozen["next_attempt_at"] = "normalized"
        frozen["created_at"] = "normalized"
        frozen["settle_body"] = (row.settle_body or "").replace(
            row.authorization_id, "normalized"
        )
        normalized_rows.append(frozen)
    assert normalized_rows[0] == normalized_rows[1]


def test_inline_settle_leaves_a_leased_outbox_row_to_its_drain_worker(
    prod_shaped_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A drain worker that claimed the row between enqueue and finalize keeps it."""
    store, db, _bt = prod_shaped_store
    auth, calls, client = _settle_with_sql_spy(store, monkeypatch, ws="ws-fold-leased")
    original_enqueue = SpannerSettleOutbox.enqueue

    def enqueue_then_lease(self: Any, row: Any, **kwargs: Any) -> Any:
        result = original_enqueue(self, row, **kwargs)
        db.settle_outbox[(row.authorization_id, row.intent_kind)]["lease_owner"] = "drain-w1"
        return result

    monkeypatch.setattr(SpannerSettleOutbox, "enqueue", enqueue_then_lease)

    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    row = db.settle_outbox[(auth.id, "settle")]
    assert row["status"] == "pending"
    assert row["lease_owner"] == "drain-w1"
    assert not any(
        sql.startswith("UPDATE tr_settle_outbox SET status=@status") for _txn, sql in calls
    ), "a leased row must not be rewritten by the inline path"
    assert "settle outbox done mark skipped" in caplog.text


def test_without_in_commit_activity_durability_the_standalone_mark_still_runs(
    fake_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No operational-analytics outbox in the commit: durability is only
    established by the post-commit index write, so the finalize transaction
    must NOT mark the intent done; the standalone mark after the index does."""
    store, db, _bt = fake_store
    auth, calls, client = _settle_with_sql_spy(store, monkeypatch, ws="ws-fold-fallback")

    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    assert db.settle_outbox[(auth.id, "settle")]["status"] == "done"
    marks = [
        txn for txn, sql in calls if sql.startswith("UPDATE tr_settle_outbox SET status=@status")
    ]
    # The finalize commit is the one that releases the hold. In this
    # configuration a later retention commit also touches the reservation,
    # so "any transaction touching tr_reservation" is NOT the finalize.
    release_txn = min(txn for txn, sql in calls if "tr_credit_balance" in sql)
    assert len(marks) == 1
    assert marks[0] != release_txn, "mark must stay post-commit when durability is post-commit"
    [record] = _settle_timing_records(caplog)
    assert isinstance(record.args, tuple)
    assert record.args[7] > 0.0  # a real standalone mark commit was timed


def test_folded_mark_defers_retention_while_a_sibling_intent_is_outstanding(
    prod_shaped_store: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settle and refund intents share the authorization; while the refund is
    still pending, the folded settle mark must leave the shared records
    TTL-ineligible exactly as the standalone mark did."""
    store, db, _bt = prod_shaped_store
    auth, _calls, client = _settle_with_sql_spy(store, monkeypatch, ws="ws-fold-sibling")
    _outbox(store).enqueue(_row(auth, intent="refund"), initial_delay_seconds=60)

    resp = client.post("/v1/internal/gateway/settle", json=_settle_json(auth.id))

    assert resp.status_code == 200, resp.text
    assert db.settle_outbox[(auth.id, "settle")]["status"] == "done"
    assert db.settle_outbox[(auth.id, "refund")]["status"] == "pending"
    assert db.gateway_authorizations[auth.id]["terminal_at"] is None
    assert db.reservations[auth.credit_reservation_id].get("terminal_at") is None
