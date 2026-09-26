from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.spanner import (
    FakeSpannerDatabase,
    _evaluate_authorization_json,
    _ParamTypes,
    _validate_json_arguments,
    make_fake_store,
)
from tests.fixtures.stage_d.storage_codec_5832dd29 import json_body as parent_json_body
from trusted_router import storage_gcp_authorize as authorize_mod
from trusted_router import storage_gcp_counter_dml as counter_dml
from trusted_router import storage_gcp_request_records as request_records
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
from trusted_router.storage_gcp_request_records import (
    insert_gateway_authorization,
    mark_gateway_authorization_settled,
    read_gateway_authorization,
)
from trusted_router.storage_gcp_stage_d import HeartbeatResult, heartbeat_gateway_atomic
from trusted_router.storage_models import GatewayAuthorization, Generation
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
    stage_d_boot_kid: str | None = None,
    database: FakeSpannerDatabase | None = None,
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
        stage_d_boot_kid=stage_d_boot_kid,
        app_id="app-stage-d" if app_markup_basis_points else "",
        app_markup_basis_points=app_markup_basis_points,
        app_owner_user_id="owner-stage-d" if app_markup_basis_points else "",
        receipt_fee_basis_points=receipt_fee_basis_points,
    )
    db = database or FakeSpannerDatabase(now=NOW)

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


def _boot(kid: str, image_digest: str) -> tuple[Ed25519PrivateKey, SpendLeaseBoot]:
    private = Ed25519PrivateKey.generate()
    return private, SpendLeaseBoot(
        kid=kid,
        jwk={
            "kty": "OKP",
            "crv": "Ed25519",
            "x": b64url_encode(private.public_key().public_bytes_raw()),
        },
        approved=True,
        verified=True,
        image_digest=image_digest,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-09-02T00:00:00Z",
    )


def _boot_auth_header(
    private: Ed25519PrivateKey,
    boot: SpendLeaseBoot,
    raw_body: bytes,
) -> str:
    signature = private.sign(
        boot_auth_digest("POST", "/v1/internal/gateway/heartbeat", raw_body)
    )
    return f"kid={boot.kid},sig={b64url_encode(signature)}"


def _gateway_heartbeat_store(
    stage_d_boot_kid: str | None,
    *boots: SpendLeaseBoot,
) -> FakeSpannerDatabase:
    store, db, _table = make_fake_store(request_record_write_mode="typed")
    db.now = NOW
    configure_store(store)
    _seed(stage_d_boot_kid=stage_d_boot_kid, database=db)
    for boot in boots:
        store.observe_spend_lease_boot(boot)
    return db


def _assert_heartbeat_state_unchanged(db: FakeSpannerDatabase) -> None:
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    assert stored["heartbeat_seq"] == 0
    assert stored["delivered_usage"] is None


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
    authorization = GatewayAuthorization(
        id="gwa-stage-d-fixture",
        workspace_id="workspace",
        key_hash="key",
        model_id="model",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=300,
        stage_d_boot_kid=boot.kid,
    )
    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_gateway_authorization",
        lambda _self, _authorization_id: authorization,
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
    # Authorization binds the immutable kid. The now-live set is deliberately
    # absent and must not be consulted for an in-flight request.
    settings = Settings(environment="test")

    accepted = gateway._heartbeat_gateway_sync(_request(header), body, settings, raw)
    assert accepted == _json("heartbeat_response_accepted.json")

    wrong_kid_header = f"kid=other-boot,sig={b64url_encode(signature)}"
    with pytest.raises(HTTPException) as wrong_kid:
        gateway._heartbeat_gateway_sync(
            _request(wrong_kid_header), body, settings, raw
        )
    assert wrong_kid.value.detail == _json("rejection_boot_not_accepted.json")

    with pytest.raises(HTTPException) as raised:
        gateway._heartbeat_gateway_sync(_request(header), body, settings, raw + b" ")
    assert raised.value.detail == _json("rejection_boot_not_accepted.json")


def test_heartbeat_rejects_valid_current_boot_when_persisted_kid_differs() -> None:
    _private_a, boot_a = _boot("boot-a", "sha256:" + "aa" * 32)
    private_b, boot_b = _boot("boot-b", "sha256:" + "bb" * 32)
    db = _gateway_heartbeat_store(boot_a.kid, boot_a, boot_b)
    raw = _literal("heartbeat_request.json")
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    settings = Settings(
        environment="test",
        spend_lease_accepted_gcp_image_digests=boot_b.image_digest,
    )
    assert boot_b.image_digest in settings.spend_lease_accepted_gcp_digests

    with pytest.raises(HTTPException) as raised:
        gateway._heartbeat_gateway_sync(
            _request(_boot_auth_header(private_b, boot_b, raw)), body, settings, raw
        )

    assert raised.value.detail == _json("rejection_boot_not_accepted.json")
    _assert_heartbeat_state_unchanged(db)


def test_heartbeat_rejects_pre_stage_d_authorization_without_boot_kid() -> None:
    private_a, boot_a = _boot("boot-a", "sha256:" + "aa" * 32)
    db = _gateway_heartbeat_store(None, boot_a)
    raw = _literal("heartbeat_request.json")
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    settings = Settings(
        environment="test",
        spend_lease_accepted_gcp_image_digests=boot_a.image_digest,
    )

    with pytest.raises(HTTPException) as raised:
        gateway._heartbeat_gateway_sync(
            _request(_boot_auth_header(private_a, boot_a, raw)), body, settings, raw
        )

    assert raised.value.detail == _json("rejection_boot_not_accepted.json")
    _assert_heartbeat_state_unchanged(db)


def test_heartbeat_accepts_persisted_boot_kid_after_live_set_rotates() -> None:
    private_a, boot_a = _boot("boot-a", "sha256:" + "aa" * 32)
    _private_b, boot_b = _boot("boot-b", "sha256:" + "bb" * 32)
    db = _gateway_heartbeat_store(boot_a.kid, boot_a, boot_b)
    raw = _literal("heartbeat_request.json")
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    settings = Settings(
        environment="test",
        spend_lease_accepted_gcp_image_digests=boot_b.image_digest,
    )
    assert boot_a.image_digest not in settings.spend_lease_accepted_gcp_digests

    response = gateway._heartbeat_gateway_sync(
        _request(_boot_auth_header(private_a, boot_a, raw)), body, settings, raw
    )

    assert response["accepted"] is True
    stored = db.gateway_authorizations["gwa-stage-d-fixture"]
    assert stored["heartbeat_seq"] == 1
    assert json.loads(stored["delivered_usage"]) == _usage()


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
        stage_d_boot_kid=boot.kid,
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
    settings = Settings(environment="test")

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
    assert '"TR_REAP_SNAPSHOT_BOOKING_ENABLED=false"' not in rollout


@pytest.mark.parametrize("storage", ["typed", "payload_only", "mixed", "null", "partial"])
@pytest.mark.parametrize("use_snapshot", [True, False])
@pytest.mark.parametrize("microseconds", [0, 123456])
def test_finalize_preserves_heartbeat_committed_after_s1(
    storage: str, microseconds: int, use_snapshot: bool,
) -> None:
    # Independent list: dropping any one field from the SQL must fail this test.
    fields = ("heartbeat_seq", "heartbeat_at", "heartbeat_hash", "started_at",
              "selected_endpoint_id", "delivered_usage")
    store, db, _table = make_fake_store(request_record_write_mode="typed")
    _db, initial = _seed(database=db)
    _seed_reaper_counters(db)
    snapshot = store.get_gateway_authorization(initial.id)  # S1
    assert snapshot is not None and snapshot.heartbeat_seq == 0
    started_at = NOW.replace(microsecond=microseconds)
    if storage != "null":
        assert _heartbeat(db, started_at=started_at).accepted  # commits before T3
    if storage == "partial":
        # A rolling row with only some heartbeat facts populated.
        row = db.gateway_authorizations[initial.id]
        payload = json.loads(row["payload"])
        for field in ("heartbeat_at", "heartbeat_hash", "delivered_usage"):
            row[field] = None
            payload.pop(field, None)
        row["payload"] = json.dumps(payload)
    current = store.get_gateway_authorization(initial.id)
    assert current is not None
    expected = {field: getattr(current, field) for field in fields}
    assert expected["heartbeat_seq"] == (0 if storage == "null" else 1)
    if storage in {"payload_only", "mixed"}:
        # Rolling revisions may have NULL typed columns. Their CURRENT
        # payload, not S1's payload, supplies those values to a strong read.
        row = db.gateway_authorizations[initial.id]
        payload = json.loads(row["payload"])
        for index, field in enumerate(fields):
            if storage == "payload_only" or index % 2 == 0:
                payload[field] = expected[field]
                row[field] = None
        row["payload"] = json.dumps(payload)
    generation = Generation(
        id="gen-heartbeat-race", request_id="req-heartbeat-race",
        gateway_request_id="trace-heartbeat-race", workspace_id="workspace",
        key_hash="key", model="model", provider_name="anthropic", app="",
        tokens_prompt=100, tokens_completion=10, total_cost_microdollars=100,
        usage_type=UsageType.CREDITS, speed_tokens_per_second=1,
        finish_reason="stop", status="success", streamed=True,
        created_at=NOW.isoformat(),
    )
    # The old finalize serialized the strong-read dataclass with json_body.
    # Freeze that serializer, and compute the oracle BEFORE running new SQL.
    expected_authorization = replace(current)
    expected_authorization.record_finalization(
        success=True, actual_microdollars=100,
        selected_usage_type=UsageType.CREDITS, generation=generation,
    )
    expected_payload = json.loads(parent_json_body(expected_authorization))
    result = store.typed_finalize_gateway_authorization_result(
        initial.id, success=True, actual_microdollars=100,
        selected_usage_type=UsageType.CREDITS, generation=generation,
        **({"authorization_snapshot": snapshot} if use_snapshot else {}),
    )
    assert result.finalized
    finalized_payload = json.loads(db.gateway_authorizations[initial.id]["payload"])
    assert finalized_payload.keys() == expected_payload.keys()
    assert finalized_payload == expected_payload
    assert db.gateway_authorizations[initial.id]["payload"] == parent_json_body(expected_authorization)
    with db.snapshot() as reader:
        direct = read_gateway_authorization(reader, _ParamTypes, initial.id)
    # Both strong merged store APIs (authorization and indexed evidence)
    # must expose the same facts as the raw payload and direct typed reader.
    reads = (direct, store.get_gateway_authorization(initial.id),
             store.get_gateway_authorization_by_gateway_request_id("trace-heartbeat-race"))
    for read in reads:
        assert read is not None
        assert {field: getattr(read, field) for field in fields} == expected
        assert read.settled and read.finalized_cost_microdollars == 100
    assert snapshot.heartbeat_seq == 0 and not snapshot.settled
    # Both entry paths must match the old contract, not each other's new merge.
    assert direct is not None
    assert asdict(direct) == asdict(expected_authorization)


def _sql_function_count(expression: str) -> int:
    # Ignore SQL string literals (paths and timestamp formats). Count all call
    # names, not a whitelist that could miss a newly introduced SQL function.
    unquoted = re.sub(r"'(?:[^']|'')*'", "''", expression)
    return len(re.findall(r"\b[A-Za-z_]\w*\s*\(", unquoted))


def test_finalize_payload_sql_function_budget() -> None:
    # Spanner rejects statements above 1000 functions. Leave headroom for the
    # rest of the UPDATE and future fields; the old 2**6 expansion fails here.
    assert _sql_function_count(request_records._SETTLED_PAYLOAD_SQL) <= 400


def test_finalize_payload_sql_growth_is_linear(monkeypatch: pytest.MonkeyPatch) -> None:
    fields = request_records._AUTHORIZATION_HEARTBEAT_FIELDS
    counts = []
    sizes = []
    for extra in range(7):
        monkeypatch.setattr(
            request_records, "_AUTHORIZATION_HEARTBEAT_FIELDS",
            fields + tuple(f"future_heartbeat_{index}" for index in range(extra)),
        )
        expression = request_records._settled_payload_sql()
        counts.append(_sql_function_count(expression))
        sizes.append(len(expression.encode()))
    assert counts[1] > counts[0]
    assert len({right - left for left, right in zip(counts[:-1], counts[1:], strict=True)}) == 1
    assert len({right - left for left, right in zip(sizes[:-1], sizes[1:], strict=True)}) == 1
    assert counts[-1] <= 400


@pytest.mark.parametrize("presence_mask", range(64))
@pytest.mark.parametrize("storage", ["typed", "payload_only", "mixed", "explicit_null"])
def test_finalize_payload_presence_matches_parent_serializer(
    presence_mask: int, storage: str,
) -> None:
    fields = ("heartbeat_seq", "heartbeat_at", "heartbeat_hash", "started_at",
              "selected_endpoint_id", "delivered_usage")
    db, initial = _seed()
    assert _heartbeat(db).accepted
    with db.snapshot() as reader:
        snapshot = read_gateway_authorization(reader, _ParamTypes, initial.id)
    assert snapshot is not None
    row = db.gateway_authorizations[initial.id]
    payload = json.loads(row["payload"])
    for index, field in enumerate(fields):
        payload.pop(field, None)
        if not presence_mask & (1 << index):
            row[field] = None
        elif storage == "explicit_null":
            payload[field] = None
            row[field] = None
        elif storage == "payload_only" or (storage == "mixed" and index % 2 == 0):
            payload[field] = getattr(snapshot, field)
            row[field] = None
        else:
            # Conflicting stale payload value must lose to the typed column.
            payload[field] = "stale"
    row["payload"] = json.dumps(payload)
    with db.snapshot() as reader:
        expected = read_gateway_authorization(reader, _ParamTypes, initial.id)
    assert expected is not None
    for authorization in (snapshot, expected):
        authorization.record_finalization(
            success=False, actual_microdollars=0,
            selected_usage_type=UsageType.CREDITS, generation=None,
        )
    expected_payload = json.loads(parent_json_body(expected))
    assert {field for field in fields if field in expected_payload} == {
        field for index, field in enumerate(fields)
        if storage != "explicit_null" and presence_mask & (1 << index)
    }
    assert db.run_in_transaction(
        lambda transaction: mark_gateway_authorization_settled(transaction, _ParamTypes, snapshot)
    ) == 1
    finalized_payload = json.loads(db.gateway_authorizations[initial.id]["payload"])
    assert finalized_payload.keys() == expected_payload.keys()
    assert finalized_payload == expected_payload
    assert db.gateway_authorizations[initial.id]["payload"] == parent_json_body(expected)


@pytest.mark.parametrize("field", [
    "heartbeat_seq", "heartbeat_at", "heartbeat_hash", "started_at",
    "selected_endpoint_id", "delivered_usage",
])
def test_finalize_omits_explicit_payload_json_null(field: str) -> None:
    db, authorization = _seed()
    row = db.gateway_authorizations[authorization.id]
    payload = json.loads(row["payload"])
    payload[field] = None
    row[field] = None
    row["payload"] = json.dumps(payload)
    with db.snapshot() as reader:
        reread = read_gateway_authorization(reader, _ParamTypes, authorization.id)
    assert reread is not None
    authorization = reread
    authorization.record_finalization(
        success=False, actual_microdollars=0,
        selected_usage_type=UsageType.CREDITS, generation=None,
    )
    expected_payload = json.loads(parent_json_body(authorization))
    assert field not in expected_payload
    assert db.run_in_transaction(
        lambda transaction: mark_gateway_authorization_settled(transaction, _ParamTypes, authorization)
    ) == 1
    finalized_payload = json.loads(db.gateway_authorizations[authorization.id]["payload"])
    assert finalized_payload.keys() == expected_payload.keys()
    assert finalized_payload == expected_payload
    assert db.gateway_authorizations[authorization.id]["payload"] == parent_json_body(authorization)


def _round4_payload_sql() -> str:
    # Frozen rejected construction: keep independent of the production builder.
    merged = "PARSE_JSON(@payload)"
    for column in ("heartbeat_seq", "heartbeat_at", "heartbeat_hash", "started_at",
                   "selected_endpoint_id", "delivered_usage"):
        value = column
        if column in {"started_at", "heartbeat_at"}:
            value = (
                f"FORMAT_TIMESTAMP(IF(MOD(UNIX_MICROS({column}),1000000)=0,"
                f"'%Y-%m-%dT%H:%M:%SZ','%Y-%m-%dT%H:%M:%E6SZ'),{column},'UTC')"
            )
        merged = (
            f"JSON_SET({merged},'$.{column}', IF({column} IS NULL,"
            f"JSON_QUERY(PARSE_JSON(payload),'$.{column}'),TO_JSON({value})),"
            f"create_if_missing=>{column} IS NOT NULL OR "
            f"JSON_QUERY(PARSE_JSON(payload),'$.{column}') IS NOT NULL)"
        )
    return f"TO_JSON_STRING({merged})"


def test_fake_rejects_round4_payload_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_records, "_SETTLED_PAYLOAD_SQL", _round4_payload_sql())
    db, authorization = _seed()
    with pytest.raises(ValueError, match=(
        "INVALID_ARGUMENT: Argument 'create_if_missing' to JSON_SET "
        "must be a literal or query parameter"
    )):
        db.run_in_transaction(
            lambda transaction: mark_gateway_authorization_settled(
                transaction, _ParamTypes, authorization,
            )
        )


@pytest.mark.parametrize("expression,argument,function", [
    ("JSON_REMOVE(JSON_SET(x,'$.f',v), IF(col IS NULL, '$.f', NULL))", 2, "JSON_REMOVE"),
    ("JSON_SET(x, IF(col IS NULL, '$.f', '$.g'), v)", 2, "JSON_SET"),
    ("JSON_SET(x, '$.f', v, path_column, v)", 4, "JSON_SET"),
    ("JSON_SET(JSON_SET(x,'$.f',v,create_if_missing=>TRUE),path_column,v)", 2, "JSON_SET"),
    ("JSON_QUERY(x, IF(col IS NULL, '$.f', NULL))", 2, "JSON_QUERY"),
    ("JSON_STRIP_NULLS(x, IF(col IS NULL, '$.f', NULL))", 2, "JSON_STRIP_NULLS"),
])
@pytest.mark.parametrize("operation", ["select", "update"])
def test_fake_rejects_row_dependent_json_paths(
    expression: str, argument: int, function: str, operation: str,
) -> None:
    db = FakeSpannerDatabase()
    with pytest.raises(ValueError, match=(
        f"INVALID_ARGUMENT: Argument {argument} to {function} must be a constant expression"
    )):
        if operation == "select":
            with db.snapshot() as reader:
                reader.execute_sql(f"SELECT IF(FALSE, {expression}, NULL) FROM t")  # noqa: S608
        else:
            db.run_in_transaction(
                lambda transaction: transaction.execute_update(f"UPDATE t SET x={expression}")  # noqa: S608
            )


@pytest.mark.parametrize("option", ["TRUE", "FALSE", "NULL", "@create"])
def test_fake_accepts_literal_or_parameter_json_option(option: str) -> None:
    _validate_json_arguments(f"JSON_SET(x, '$.f', v, create_if_missing=>{option})")


@pytest.mark.parametrize("option", ["col IS NULL", "IF(col IS NULL, TRUE, FALSE)"])
def test_fake_rejects_row_dependent_json_option(option: str) -> None:
    with pytest.raises(ValueError, match="must be a literal or query parameter"):
        _validate_json_arguments(f"JSON_SET(x, '$.f', v, create_if_missing=>{option})")


@pytest.mark.parametrize("function,tail", [
    ("JSON_SET", ", v"), ("JSON_REMOVE", ""), ("JSON_QUERY", ""), ("JSON_STRIP_NULLS", ""),
])
def test_fake_accepts_constant_json_path(function: str, tail: str) -> None:
    _validate_json_arguments(f"{function}(x, IF(1=1, '$.f', NULL){tail})")


@pytest.mark.parametrize("expression,expected", [
    ("JSON_TYPE(JSON_QUERY(PARSE_JSON(@payload),'$.null_value'))", "null"),
    ("JSON_TYPE(JSON_QUERY(PARSE_JSON(@payload),'$.missing'))", None),
    ("COALESCE(JSON_TYPE(JSON_QUERY(PARSE_JSON(@payload),'$.missing')),'null')", "null"),
    ("TO_JSON_STRING(PARSE_JSON(@payload))", '{"a":"quote\\\"slash\\\\","null_value":null,"z":0}'),
    ("CONCAT(SUBSTR('abc',1,LENGTH('abc')-1),'}')", "ab}"),
    ("JSON_TYPE(TO_JSON(0))", "number"),
    ("JSON_TYPE(TO_JSON(FALSE))", "boolean"),
    ("JSON_TYPE(TO_JSON(''))", "string"),
    ("TO_JSON_STRING(JSON_QUERY(PARSE_JSON(@payload),IF(1=1,'$.z',NULL)))", "0"),
])
def test_fake_evaluates_settle_json_primitives(expression: str, expected: Any) -> None:
    payload = json.dumps({"z": 0, "null_value": None, "a": 'quote"slash\\'})
    assert _evaluate_authorization_json(expression, {}, {"payload": payload}) == expected


def test_fake_parse_json_rejects_invalid_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        _evaluate_authorization_json("PARSE_JSON(@payload)", {}, {"payload": '{,"f":1}'})


@pytest.mark.parametrize("serialized", [
    "{}", "{ }", "[]", "null", ' {"id":"x"}', '{"id":"x"} ', '{,"f":1}',
    *[json.dumps({"id": "x", field: None}) for field in (
        "heartbeat_seq", "heartbeat_at", "heartbeat_hash", "started_at",
        "selected_endpoint_id", "delivered_usage",
    )],
])
def test_finalize_rejects_invalid_payload_parameter(
    serialized: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, authorization = _seed()
    original = request_records.json_body
    monkeypatch.setattr(
        request_records, "json_body",
        lambda value: serialized if isinstance(value, dict) else original(value),
    )
    before = db.transaction_execute_update_calls
    with pytest.raises(ValueError, match="nonempty JSON object without heartbeat keys"):
        db.run_in_transaction(
            lambda transaction: mark_gateway_authorization_settled(transaction, _ParamTypes, authorization)
        )
    assert db.transaction_execute_update_calls == before
