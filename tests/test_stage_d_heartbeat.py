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
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayHeartbeatRequest
from trusted_router.spend_leases import (
    SpendLeaseBoot,
    b64url_encode,
    boot_auth_digest,
)
from trusted_router.storage import configure_store
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_gcp_counter_dml import insert_reservation
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


def _request(raw_header: str | None) -> Request:
    headers = [] if raw_header is None else [(b"x-tr-boot-auth", raw_header.encode())]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/internal/gateway/heartbeat",
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


def test_canonical_fixture_hash_is_stable() -> None:
    body = GatewayHeartbeatRequest.model_validate_json(_literal("heartbeat_request.json"))
    canonical = json.dumps(
        body.model_dump(exclude_none=True), sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "a6d2b037f55f15955961bc4c875d059617ee0fb0b15d771f927329c153be7e45"
    )
