from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes, make_fake_store
from trusted_router import storage_gcp_authorize as authorize_mod
from trusted_router import storage_gcp_counter_dml as counter_dml
from trusted_router.app_markup_billing import (
    app_markup_microdollars,
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
    app_markup_payout_event_id,
)
from trusted_router.config import Settings
from trusted_router.pricing import signed_receipt_price_microdollars
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayHeartbeatRequest
from trusted_router.spend_leases import (
    SpendLeaseBoot,
    b64url_encode,
    boot_auth_digest,
)
from trusted_router.stage_d import endpoint_cost_microdollars_from_document
from trusted_router.storage import configure_store
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_gcp_authorize import (
    SettleOutcome,
    reap_expired_reservations_result,
    typed_finalize_atomic,
)
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counter_dml import insert_reservation
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_request_records import insert_gateway_authorization
from trusted_router.storage_gcp_stage_d import HeartbeatResult, heartbeat_gateway_atomic
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

FIXTURES = Path(__file__).parent / "fixtures" / "stage_d"
NOW = datetime(2026, 9, 2, tzinfo=UTC)


def _literal(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _json(name: str) -> Any:
    return json.loads(_literal(name))


def _usage(*, input_tokens: int = 100, output_tokens: int = 10) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "price_tier_input_tokens": 0,
        "reasoning_tokens": 0,
    }


def _seed(
    *,
    settled: bool = False,
    cohort: bool = True,
    heartbeat_seq: int = 0,
    heartbeat_hash: str | None = None,
    delivered_usage: dict[str, int] | None = None,
    app_markup_basis_points: int = 0,
    receipt_fee_basis_points: int = 0,
) -> tuple[FakeSpannerDatabase, GatewayAuthorization]:
    authorization = GatewayAuthorization(
        id="gwa-stage-d-fixture",
        workspace_id="workspace",
        key_hash="key",
        model_id="model",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=300,
        credit_reservation_id="reservation",
        settled=settled,
        pricing_snapshot=(
            _literal("pricing_document.json").decode().strip() if cohort else None
        ),
        heartbeat_seq=heartbeat_seq if cohort else None,
        heartbeat_hash=heartbeat_hash,
        selected_endpoint_id="anthropic/test" if heartbeat_seq else None,
        delivered_usage=(
            json.dumps(delivered_usage, sort_keys=True, separators=(",", ":"))
            if delivered_usage is not None
            else None
        ),
        stage_d_reason="ok" if cohort else "not_streaming",
        stage_d_prompt_tokens=100,
        stage_d_max_output_tokens=100,
        app_id="app-stage-d" if app_markup_basis_points else "",
        app_markup_basis_points=app_markup_basis_points,
        app_owner_user_id="owner-stage-d" if app_markup_basis_points else "",
        receipt_fee_basis_points=receipt_fee_basis_points,
    )
    db = FakeSpannerDatabase(now=NOW)

    def seed(transaction: Any) -> None:
        insert_reservation(
            transaction,
            _ParamTypes,
            reservation_id="reservation",
            workspace_id="workspace",
            key_hash="key",
            ws_shard=0,
            credit_shard=0,
            key_shard=0,
            credit_reserved_micro=300,
            key_reserved_micro=300,
            hold_usage_type="Credits",
            authorization_id=authorization.id,
            idempotency_scope=None,
            idempotency_fingerprint=None,
            expires_at=NOW + timedelta(seconds=30),
            created_at=NOW,
        )
        insert_gateway_authorization(
            transaction,
            _ParamTypes,
            authorization,
            created_at=NOW,
        )

    db.run_in_transaction(seed)
    if settled:
        db.reservations["reservation"]["settled"] = True
    return db, authorization


def _heartbeat(
    db: FakeSpannerDatabase,
    *,
    seq: int = 1,
    endpoint_id: str = "anthropic/test",
    usage: dict[str, int] | None = None,
    payload_hash: str = "a" * 64,
    started_at: datetime = NOW,
) -> HeartbeatResult:
    return heartbeat_gateway_atomic(
        db,
        _ParamTypes,
        authorization_id="gwa-stage-d-fixture",
        seq=seq,
        started_at=started_at,
        selected_endpoint_id=endpoint_id,
        usage=usage or _usage(),
        heartbeat_hash=payload_hash,
        stream=True,
        grace_seconds=300,
        now=NOW,
    )


def test_literal_stage_d_fixtures_parse_without_model_rebuilding() -> None:
    names = [
        "pricing_document.json",
        "heartbeat_request.json",
        "heartbeat_request_duplicate.json",
        "heartbeat_response_accepted.json",
        "heartbeat_response_duplicate.json",
        "authorize_response_eligible.json",
        "authorize_response_ineligible.json",
        "settle_response_finalized.json",
        "settle_response_intent_durable.json",
        "settle_response_already_finalized.json",
        "settle_response_reaped_snapshot.json",
        "refund_response_finalized.json",
        "refund_response_intent_durable.json",
        "refund_response_already_finalized.json",
        "refund_response_reaped_snapshot.json",
        "disposition_lookup_response.json",
        "late_settle_after_reaped_snapshot_response.json",
        *(f"rejection_{reason}.json" for reason in (
            "unknown_authorization",
            "already_terminal",
            "out_of_cohort",
            "boot_not_accepted",
            "stale_seq",
            "endpoint_mismatch",
            "usage_regression",
            "usage_exceeds_cap",
        )),
    ]
    assert all(isinstance(json.loads(_literal(name)), dict) for name in names)
    assert _literal("heartbeat_request.json") == _literal("heartbeat_request_duplicate.json")
    assert _literal("heartbeat_response_accepted.json") == _literal(
        "heartbeat_response_duplicate.json"
    )


def test_heartbeat_accepts_updates_and_renews_with_greatest() -> None:
    db, _authorization = _seed()

    result = _heartbeat(db)

    assert {
        "accepted": result.accepted,
        "seq": result.seq,
        "expires_at_ms": result.expires_at_ms,
        "cap_micro": result.cap_micro,
        "running_micro": result.running_micro,
    } == _json("heartbeat_response_accepted.json")
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    assert stored["heartbeat_seq"] == 1
    assert stored["started_at"] == NOW
    assert stored["selected_endpoint_id"] == "anthropic/test"
    assert db.reservations["reservation"]["expires_at"] == NOW + timedelta(seconds=300)


def test_heartbeat_renewal_never_shortens_a_later_expiry() -> None:
    db, _authorization = _seed()
    later_expiry = NOW + timedelta(seconds=600)
    db.reservations["reservation"]["expires_at"] = later_expiry

    assert _heartbeat(db).accepted

    assert db.reservations["reservation"]["expires_at"] == later_expiry


def test_heartbeat_running_charge_includes_frozen_receipt_and_app_fees() -> None:
    db, _authorization = _seed(
        app_markup_basis_points=1_250,
        receipt_fee_basis_points=1_200,
    )
    base = endpoint_cost_microdollars_from_document(
        _json("pricing_document.json"),
        "anthropic/test",
        100,
        10,
    )
    receipt_charge = signed_receipt_price_microdollars(base, 1_200)
    expected = receipt_charge + app_markup_microdollars(receipt_charge, 1_250)

    result = _heartbeat(db)

    assert result.accepted
    assert result.running_micro == expected


def test_heartbeat_rejects_a_monetary_overrun_without_renewing() -> None:
    db, _authorization = _seed(
        app_markup_basis_points=1_250,
        receipt_fee_basis_points=1_200,
    )
    expires_at = db.reservations["reservation"]["expires_at"]

    result = _heartbeat(db, usage=_usage(output_tokens=100))

    assert result.accepted is False
    assert result.reason == "usage_exceeds_cap"
    assert db.gateway_authorizations["gwa-stage-d-fixture"]["heartbeat_seq"] == 0
    assert db.reservations["reservation"]["expires_at"] == expires_at


def test_exact_duplicate_replay_is_read_only_and_returns_same_response() -> None:
    db, _authorization = _seed()
    first = _heartbeat(db)
    update_calls = db.transaction_execute_update_calls
    versions = (
        db.gateway_authorization_versions["gwa-stage-d-fixture"],
        db.reservation_versions["reservation"],
    )

    replay = _heartbeat(db)

    assert replay == replace(first, replay=True)
    assert db.transaction_execute_update_calls == update_calls
    assert versions == (
        db.gateway_authorization_versions["gwa-stage-d-fixture"],
        db.reservation_versions["reservation"],
    )


def test_started_at_and_selected_endpoint_are_write_once() -> None:
    db, _authorization = _seed()
    assert _heartbeat(db).accepted

    second = _heartbeat(
        db,
        seq=2,
        usage=_usage(output_tokens=20),
        payload_hash="b" * 64,
        started_at=NOW + timedelta(minutes=1),
    )

    assert second.accepted
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    assert stored["started_at"] == NOW
    assert stored["selected_endpoint_id"] == "anthropic/test"


def test_second_priced_candidate_is_rejected_without_renewal() -> None:
    db, _authorization = _seed(
        heartbeat_seq=1,
        heartbeat_hash="a" * 64,
        delivered_usage=_usage(),
    )
    document = _json("pricing_document.json")
    document["candidates"].append(
        {**document["candidates"][0], "endpoint_id": "anthropic/second"}
    )
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    stored["pricing_snapshot"] = json.dumps(document, sort_keys=True, separators=(",", ":"))
    expires_at = db.reservations["reservation"]["expires_at"]

    result = _heartbeat(
        db,
        seq=2,
        endpoint_id="anthropic/second",
        usage=_usage(output_tokens=20),
        payload_hash="b" * 64,
    )

    assert result.reason == "endpoint_mismatch"
    assert stored["heartbeat_seq"] == 1
    assert stored["selected_endpoint_id"] == "anthropic/test"
    assert db.reservations["reservation"]["expires_at"] == expires_at


@pytest.mark.parametrize(
    ("reason", "prepare", "call"),
    [
        (
            "unknown_authorization",
            lambda: FakeSpannerDatabase(now=NOW),
            lambda db: _heartbeat(db),
        ),
        (
            "already_terminal",
            lambda: _seed(settled=True)[0],
            lambda db: _heartbeat(db),
        ),
        (
            "out_of_cohort",
            lambda: _seed(cohort=False)[0],
            lambda db: _heartbeat(db),
        ),
        (
            "stale_seq",
            lambda: _seed(
                heartbeat_seq=2,
                heartbeat_hash="b" * 64,
                delivered_usage=_usage(output_tokens=20),
            )[0],
            lambda db: _heartbeat(db, seq=1),
        ),
        (
            "endpoint_mismatch",
            lambda: _seed(
                heartbeat_seq=1,
                heartbeat_hash="a" * 64,
                delivered_usage=_usage(),
            )[0],
            lambda db: _heartbeat(db, seq=2, endpoint_id="other", payload_hash="b" * 64),
        ),
        (
            "usage_regression",
            lambda: _seed(
                heartbeat_seq=1,
                heartbeat_hash="a" * 64,
                delivered_usage=_usage(output_tokens=10),
            )[0],
            lambda db: _heartbeat(
                db,
                seq=2,
                usage=_usage(output_tokens=9),
                payload_hash="b" * 64,
            ),
        ),
        (
            "usage_exceeds_cap",
            lambda: _seed()[0],
            lambda db: _heartbeat(db, usage=_usage(output_tokens=101)),
        ),
    ],
)
def test_each_transaction_rejection_reason(
    reason: str,
    prepare: Any,
    call: Any,
) -> None:
    result = call(prepare())
    assert result.accepted is False
    assert result.reason == reason
    rendered = gateway._heartbeat_rejection(reason)
    assert isinstance(rendered, HTTPException)
    assert rendered.detail == _json(f"rejection_{reason}.json")


def test_same_sequence_with_different_hash_is_stale() -> None:
    db, _authorization = _seed()
    assert _heartbeat(db).accepted
    result = _heartbeat(db, payload_hash="b" * 64)
    assert result.reason == "stale_seq"


@pytest.mark.parametrize(
    "component",
    [
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "price_tier_input_tokens",
        "reasoning_tokens",
    ],
)
def test_every_usage_component_is_monotone(component: str) -> None:
    stored = _usage()
    stored[component] = 10
    attempted = dict(stored)
    attempted[component] = 9
    db, _authorization = _seed(
        heartbeat_seq=1,
        heartbeat_hash="a" * 64,
        delivered_usage=stored,
    )

    result = _heartbeat(
        db,
        seq=2,
        usage=attempted,
        payload_hash="b" * 64,
    )

    assert result.reason == "usage_regression"


def _request(
    raw_header: str | None,
    *,
    method: str = "POST",
    path: str = "/v1/internal/gateway/heartbeat",
) -> Request:
    headers = [] if raw_header is None else [(b"x-tr-boot-auth", raw_header.encode())]
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers,
        }
    )


def test_heartbeat_boot_auth_uses_exact_literal_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _table = make_fake_store(request_record_write_mode="typed")
    configure_store(store)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    boot = SpendLeaseBoot(
        kid="boot-stage-d",
        jwk={"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public)},
        approved=True,
        verified=True,
        image_digest="sha256:" + "12" * 32,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-09-02T00:00:00Z",
    )
    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_spend_lease_boot",
        lambda _self, _kid: boot,
    )
    monkeypatch.setattr(
        SpannerBigtableStore,
        "heartbeat_gateway_typed",
        lambda _self, **_kwargs: HeartbeatResult(
            accepted=True,
            seq=1,
            expires_at_ms=1_788_307_500_000,
            cap_micro=300,
            running_micro=120,
        ),
    )
    raw = _literal("heartbeat_request.json")
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    signature = private.sign(
        boot_auth_digest("POST", "/v1/internal/gateway/heartbeat", raw)
    )
    header = f"kid={boot.kid},sig={b64url_encode(signature)}"
    settings = Settings(
        environment="test",
        spend_lease_accepted_gcp_image_digests=boot.image_digest,
    )

    accepted = gateway._heartbeat_gateway_sync(_request(header), body, settings, raw)
    assert accepted == _json("heartbeat_response_accepted.json")

    with pytest.raises(HTTPException) as raised:
        gateway._heartbeat_gateway_sync(_request(header), body, settings, raw + b" ")
    assert raised.value.detail == _json("rejection_boot_not_accepted.json")


def test_heartbeat_flag_defaults_on_and_can_disable_endpoint() -> None:
    assert Settings(environment="test").stage_d_heartbeat_enabled is True
    settings = Settings(environment="test", stage_d_heartbeat_enabled=False)
    assert settings.stage_d_heartbeat_enabled is False


def test_disposition_lookup_uses_heartbeat_boot_verifier_and_literal_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, _table = make_fake_store(request_record_write_mode="typed")
    configure_store(store)
    private = Ed25519PrivateKey.generate()
    boot = SpendLeaseBoot(
        kid="boot-stage-d-disposition",
        jwk={
            "kty": "OKP",
            "crv": "Ed25519",
            "x": b64url_encode(private.public_key().public_bytes_raw()),
        },
        approved=True,
        verified=True,
        image_digest="sha256:" + "34" * 32,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-09-02T00:00:00Z",
    )
    authorization = GatewayAuthorization(
        id="gwa-stage-d-fixture",
        workspace_id="workspace",
        key_hash="key",
        model_id="model",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=300,
        settled=True,
        finalization_outcome="reaped_snapshot",
        finalized_cost_microdollars=120,
    )
    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_spend_lease_boot",
        lambda _self, _kid: boot,
    )
    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_gateway_authorization",
        lambda _self, _authorization_id: authorization,
    )
    path = "/v1/internal/gateway/authorizations/gwa-stage-d-fixture/disposition"
    signature = private.sign(boot_auth_digest("GET", path, b""))
    header = f"kid={boot.kid},sig={b64url_encode(signature)}"
    settings = Settings(
        environment="test",
        spend_lease_accepted_gcp_image_digests=boot.image_digest,
    )

    response = gateway._gateway_authorization_disposition_sync(
        _request(header, method="GET", path=path),
        authorization.id,
        settings,
        b"",
    )

    assert response == _json("disposition_lookup_response.json")


def test_canonical_fixture_hash_is_stable() -> None:
    body = GatewayHeartbeatRequest.model_validate_json(_literal("heartbeat_request.json"))
    canonical = json.dumps(
        body.model_dump(exclude_none=True), sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "a6d2b037f55f15955961bc4c875d059617ee0fb0b15d771f927329c153be7e45"
    )


def _seed_reaper_counters(db: FakeSpannerDatabase, *, hold: int = 300) -> None:
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[("workspace", 0)] = {
        "workspace_id": "workspace",
        "shard": 0,
        "total_credits": 1_000,
        "total_usage": 0,
        "reserved": hold,
        "source_updated_at": None,
        "updated_at": None,
    }
    db.typed.setdefault(KEY_LIMIT_TABLE, {})[("key", 0)] = {
        "key_hash": "key",
        "shard": 0,
        "limit_microdollars": 1_000,
        "include_byok": True,
        "usage": 0,
        "byok_usage": 0,
        "reserved": hold,
        "day_usage": 0,
        "day_start": None,
        "week_usage": 0,
        "week_start": None,
        "month_usage": 0,
        "month_start": None,
    }


def test_reaper_strong_reread_skips_a_heartbeat_renewed_after_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    reap_now = NOW + timedelta(seconds=31)
    real_finalize = authorize_mod._finalize_reaped_reservation_atomic
    calls = 0

    def renew_then_finalize(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        assert _heartbeat(db).accepted
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(
        authorize_mod,
        "_finalize_reaped_reservation_atomic",
        renew_then_finalize,
    )

    result = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=reap_now,
        snapshot_booking_enabled=True,
    )

    assert calls == 1
    assert result.count == 0
    assert result.outcome_counts == {
        SettleOutcome.NOT_ELIGIBLE: 1,
        SettleOutcome.OUTBOX_GUARDED: 0,
        SettleOutcome.GUARD_LOST: 0,
        SettleOutcome.ERROR: 0,
        "refunded": 0,
        "snapshot_booked": 0,
    }
    assert db.reservations["reservation"]["settled"] is False
    assert db.reservations["reservation"]["expires_at"] > reap_now
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["reserved"] == 300


def test_reaper_claim_guard_records_renewal_after_strong_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    reap_now = NOW + timedelta(seconds=31)
    renewed_expiry = reap_now + timedelta(minutes=5)
    real_claim = counter_dml.claim_reservation
    calls = 0

    def renew_then_claim(
        transaction: Any,
        param_types: Any,
        reservation_id: str,
        **kwargs: Any,
    ) -> bool:
        nonlocal calls
        calls += 1
        pinned = transaction.row_snapshots[("res", reservation_id)]
        assert pinned is not None
        pinned["expires_at"] = renewed_expiry
        db.reservations[reservation_id]["expires_at"] = renewed_expiry
        return real_claim(
            transaction,
            param_types,
            reservation_id,
            **kwargs,
        )

    monkeypatch.setattr(counter_dml, "claim_reservation", renew_then_claim)

    result = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=reap_now,
        snapshot_booking_enabled=True,
    )

    assert calls == 1
    assert result.count == 0
    assert result.not_eligible == 0
    assert result.guard_lost == 1
    assert result.errors == 0
    assert result.refunded == 0
    assert result.snapshot_bookings == 0
    assert db.reservations["reservation"]["settled"] is False
    assert db.reservations["reservation"]["expires_at"] == renewed_expiry
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["reserved"] == 300
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == 0
    assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["reserved"] == 300
    assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["usage"] == 0
    assert db.gateway_authorizations["gwa-stage-d-fixture"]["settled"] is False


@pytest.mark.parametrize("status", ["pending", "dead"])
def test_reaper_atomic_guards_one_pending_or_dead_outbox_row(status: str) -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    assert _heartbeat(db).accepted
    db.settle_outbox[("gwa-stage-d-fixture", "settle")] = {
        "authorization_id": "gwa-stage-d-fixture",
        "intent_kind": "settle",
        "status": status,
    }
    reservation_before = dict(db.reservations["reservation"])
    authorization_before = dict(db.gateway_authorizations["gwa-stage-d-fixture"])
    credit_before = dict(db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)])
    key_before = dict(db.typed[KEY_LIMIT_TABLE][("key", 0)])

    result = authorize_mod._finalize_reaped_reservation_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        reap_now=NOW + timedelta(seconds=301),
        guard_outbox=True,
        snapshot_booking_enabled=True,
        operational_analytics_outbox=None,
    )

    assert len(db.settle_outbox) == 1
    assert result.outcome == SettleOutcome.OUTBOX_GUARDED
    assert result.snapshot_booked is False
    assert db.reservations["reservation"] == reservation_before
    assert db.gateway_authorizations["gwa-stage-d-fixture"] == authorization_before
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)] == credit_before
    assert db.typed[KEY_LIMIT_TABLE][("key", 0)] == key_before
    assert db.generation_records == {}


def test_reaper_atomic_release_approved_outbox_row_is_not_a_guard() -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    assert _heartbeat(db).accepted
    db.settle_outbox[("gwa-stage-d-fixture", "settle")] = {
        "authorization_id": "gwa-stage-d-fixture",
        "intent_kind": "settle",
        "status": "release_approved",
    }

    result = authorize_mod._finalize_reaped_reservation_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        reap_now=NOW + timedelta(seconds=301),
        guard_outbox=True,
        snapshot_booking_enabled=True,
        operational_analytics_outbox=None,
    )

    assert len(db.settle_outbox) == 1
    assert result.outcome == SettleOutcome.SETTLED
    assert result.snapshot_booked is True
    assert db.reservations["reservation"]["settled"] is True
    assert db.gateway_authorizations["gwa-stage-d-fixture"][
        "finalization_outcome"
    ] == "reaped_snapshot"


def test_reaper_snapshot_books_the_frozen_pricing_function_and_generation() -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    assert _heartbeat(db).accepted
    reap_now = NOW + timedelta(seconds=301)
    document = _json("pricing_document.json")
    expected = endpoint_cost_microdollars_from_document(
        document,
        "anthropic/test",
        100,
        10,
    )

    result = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=reap_now,
        snapshot_booking_enabled=True,
    )

    assert expected == 120
    assert result.count == 1
    assert result.released_hold_micro == 300
    assert result.started_markers == 1
    assert result.snapshot_bookings == 1
    assert result.refunded == 0
    assert result.outcome_counts["snapshot_booked"] == 1
    assert db.reservations["reservation"]["actual_micro"] == expected
    credit = db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]
    assert credit["reserved"] == 0
    assert credit["total_usage"] == expected
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    assert stored["finalization_outcome"] == "reaped_snapshot"
    assert stored["finalized_cost_microdollars"] == expected
    assert stored["payload"] is not None
    assert len(db.generation_records) == 1
    generation = json.loads(next(iter(db.generation_records.values()))["payload"])
    assert generation["settled_from"] == "heartbeat"
    assert generation["usage_estimated"] is True
    assert generation["tokens_prompt"] == 100
    assert generation["tokens_completion"] == 10


def test_reaper_snapshot_preserves_downstream_fees_and_app_payout() -> None:
    db, _authorization = _seed(
        app_markup_basis_points=1_250,
        receipt_fee_basis_points=1_200,
    )
    _seed_reaper_counters(db)
    heartbeat = _heartbeat(db)
    assert heartbeat.accepted
    assert heartbeat.running_micro is not None

    result = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=NOW + timedelta(seconds=301),
        snapshot_booking_enabled=True,
    )

    charge = heartbeat.running_micro
    markup = app_markup_microdollars_from_charge(charge, 1_250)
    payout = app_markup_owner_share_microdollars(markup)
    assert result.snapshot_bookings == 1
    assert db.reservations["reservation"]["actual_micro"] == charge
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == charge
    generation = json.loads(next(iter(db.generation_records.values()))["payload"])
    assert generation["total_cost_microdollars"] == charge
    assert generation["app_markup_microdollars"] == markup
    movement_id = app_markup_payout_event_id("gwa-stage-d-fixture")
    movement = db.typed["tr_credit_movement"][("user:owner-stage-d", movement_id)]
    assert movement["amount_microdollars"] == payout
    assert movement["custom_model_id"] == "app-stage-d"
    assert db.typed["tr_earnings_balance"][("owner-stage-d", 0)][
        "total_earned"
    ] == payout


def test_reaper_snapshot_clamps_a_spend_lease_to_allocation_and_hold() -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    assert _heartbeat(db).accepted
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    payload = json.loads(stored["payload"])
    payload.update(
        settlement="spend_lease",
        spend_lease_allocated_micro=80,
        spend_lease_id="lease",
    )
    stored["payload"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    stored["spend_lease_allocated_micro"] = 80
    stored["spend_lease_id"] = "lease"

    result = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=NOW + timedelta(seconds=301),
        snapshot_booking_enabled=True,
    )

    assert result.snapshot_bookings == 1
    assert db.reservations["reservation"]["actual_micro"] == 80
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == 80
    assert db.gateway_authorizations["gwa-stage-d-fixture"][
        "finalized_cost_microdollars"
    ] == 80


def test_reaper_flag_off_refunds_started_request_without_nulling_payload() -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    assert _heartbeat(db).accepted

    result = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=NOW + timedelta(seconds=301),
        snapshot_booking_enabled=False,
    )

    assert result.count == 1
    assert result.started_markers == 1
    assert result.snapshot_bookings == 0
    assert result.refunded == 1
    assert result.outcome_counts["refunded"] == 1
    assert db.reservations["reservation"]["actual_micro"] == 0
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == 0
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    assert stored["finalization_outcome"] == "refunded"
    payload = json.loads(stored["payload"])
    assert payload["pricing_snapshot"] == _literal("pricing_document.json").decode().strip()
    assert payload["selected_endpoint_id"] == "anthropic/test"
    assert payload["delivered_usage"] is not None


@pytest.mark.parametrize(
    "guard_name",
    [
        "mark_gateway_authorization_settled",
        "complete_reservation_retention",
        "complete_gateway_authorization_retention",
    ],
)
def test_reaper_lost_write_guard_rolls_back_every_money_write(
    monkeypatch: pytest.MonkeyPatch,
    guard_name: str,
) -> None:
    db, _authorization = _seed()
    _seed_reaper_counters(db)
    assert _heartbeat(db).accepted
    monkeypatch.setattr(authorize_mod, guard_name, lambda *_args, **_kwargs: 0)

    result = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=NOW + timedelta(seconds=301),
        snapshot_booking_enabled=True,
    )

    assert result.count == 0
    assert result.guard_lost == 1
    assert result.errors == 0
    assert db.reservations["reservation"]["settled"] is False
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["reserved"] == 300
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == 0
    assert db.gateway_authorizations["gwa-stage-d-fixture"]["settled"] is False
    assert db.generation_records == {}


def test_refund_before_reap_wins_the_reservation_and_authorization_guards() -> None:
    db, authorization = _seed()
    _seed_reaper_counters(db)
    authorization.record_finalization(
        success=False,
        actual_microdollars=0,
        selected_usage_type=UsageType.CREDITS,
        generation=None,
    )
    refunded = typed_finalize_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        authorization_id=authorization.id,
        success=False,
        actual_micro=0,
        settled_usage_type="Credits",
        now=NOW + timedelta(seconds=1),
        outbox_available=True,
        authorization=authorization,
        auth_body_settled=json_body(authorization),
    )
    assert refunded["outcome"] == "settled"

    reaped = reap_expired_reservations_result(
        db,
        _ParamTypes,
        now=NOW + timedelta(seconds=301),
        snapshot_booking_enabled=True,
    )

    assert reaped.count == 0
    assert db.reservations["reservation"]["actual_micro"] == 0
    assert db.gateway_authorizations[authorization.id]["finalization_outcome"] == "refunded"


def test_disposition_mapping_covers_every_terminal_and_deferred_state() -> None:
    _db, authorization = _seed()
    assert gateway._current_disposition(authorization) == "intent_durable"
    authorization.settled = True
    authorization.finalization_outcome = "settled"
    assert gateway._current_disposition(authorization) == "already_finalized"
    authorization.finalization_outcome = "refunded"
    assert gateway._current_disposition(authorization) == "already_finalized"
    authorization.finalization_outcome = "reaped_snapshot"
    assert gateway._current_disposition(authorization) == "reaped_snapshot"


def test_literal_deferred_and_terminal_disposition_responses_match_router_helpers() -> None:
    _db, authorization = _seed()
    deferred = {"data": gateway._intent_durable_gateway_data(authorization)}
    assert deferred == _json("settle_response_intent_durable.json")
    assert deferred == _json("refund_response_intent_durable.json")

    authorization.settled = True
    authorization.finalization_outcome = "settled"
    authorization.finalized_cost_microdollars = 120
    authorization.finalized_generation_id = "gen-stage-d-fixture"
    authorization.finalized_usage_type = "Credits"
    authorization.finalized_model_id = "model"
    authorization.finalized_provider = "anthropic"
    authorization.finalized_region = "us"
    authorization.finalized_input_tokens = 100
    authorization.finalized_output_tokens = 10
    settled = {"data": gateway._already_settled_gateway_data(authorization)}
    assert settled == _json("settle_response_already_finalized.json")

    authorization.finalization_outcome = "refunded"
    authorization.finalized_cost_microdollars = 0
    refunded = {"data": gateway._already_settled_gateway_data(authorization)}
    assert refunded == _json("refund_response_already_finalized.json")

    authorization.finalization_outcome = "reaped_snapshot"
    authorization.finalized_cost_microdollars = 120
    reaped = {"data": gateway._already_settled_gateway_data(authorization)}
    assert reaped == _json("settle_response_reaped_snapshot.json")
    assert reaped == _json("refund_response_reaped_snapshot.json")
    assert reaped == _json("late_settle_after_reaped_snapshot_response.json")

    assert _json("settle_response_finalized.json")["data"]["disposition"] == "finalized"
    assert _json("refund_response_finalized.json")["data"]["disposition"] == "finalized"


def test_reaper_flag_defaults_off_and_rollout_pins_it_on() -> None:
    assert Settings(environment="test").reap_snapshot_booking_enabled is False
    rollout = (Path(__file__).parents[1] / "scripts" / "deploy" / "rollout.sh").read_text()
    assert '"TR_REAP_SNAPSHOT_BOOKING_ENABLED=true"' in rollout
