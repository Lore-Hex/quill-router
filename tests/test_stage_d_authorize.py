from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.requests import Request

from tests.fakes.spanner import make_fake_store
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, effective_endpoint
from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.routes.internal.gateway import (
    _gateway_stage_d_payload,
    _stage_d_eligibility_reason,
)
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.stage_d import canonical_pricing_snapshot, endpoint_pricing_document
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, GatewayAuthorization, Workspace
from trusted_router.types import UsageType

FIXTURES = Path(__file__).parent / "fixtures" / "stage_d"


def _credit_candidate() -> tuple[Model, ModelEndpoint]:
    endpoint = effective_endpoint(
        next(endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.usage_type == "Credits")
    )
    return MODELS[endpoint.model_id], endpoint


def test_invocation_nonce_is_limited_to_64_characters() -> None:
    GatewayAuthorizeRequest(api_key_hash="hash", model="model", invocation_nonce="n" * 64)
    with pytest.raises(ValueError):
        GatewayAuthorizeRequest(api_key_hash="hash", model="model", invocation_nonce="n" * 65)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, "ok"),
        ({"pilot_workspace_ids": frozenset({"other"})}, "workspace_not_pilot"),
        ({"heartbeat_enabled": False}, "heartbeats_disabled"),
        ({"boot_accepted": False}, "boot_not_accepted"),
        ({"stream": None}, "not_streaming"),
        ({"route_type": "embeddings"}, "route"),
        ({"mixed": True}, "mixed_usage_type"),
        ({"standard_endpoint_pricing": False}, "pricing_kind"),
        ({"service_tier": "priority"}, "service_tier"),
        ({"settlement_backend": False}, "settlement_backend"),
        ({"eligibility_enabled": False}, "stage_d_disabled"),
    ],
)
def test_stage_d_eligibility_has_each_closed_reason(
    overrides: dict[str, object], reason: str
) -> None:
    model, credits = _credit_candidate()
    candidates = [(model, credits)]
    if overrides.pop("mixed", False):
        byok = next(endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.usage_type == "BYOK")
        candidates.append((MODELS[byok.model_id], byok))
    kwargs = {
        "stream": True,
        "route_type": "chat.completions",
        "endpoint_candidates": candidates,
        "standard_endpoint_pricing": True,
        "service_tier": None,
        "settlement_backend": True,
        "boot_accepted": True,
        **overrides,
    }
    eligibility_enabled = bool(kwargs.pop("eligibility_enabled", True))
    assert _stage_d_eligibility_reason(eligibility_enabled=eligibility_enabled, **kwargs) == reason  # type: ignore[arg-type]


def test_typed_authorize_inserts_cohort_sequence_zero_and_snapshot() -> None:
    store, db, _table = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="stage-d-workspace", name="Stage D", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 1_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id,
        name="stage-d",
        creator_user_id=workspace.owner_user_id,
        limit_microdollars=1_000_000,
    )
    model, endpoint = _credit_candidate()
    snapshot = canonical_pricing_snapshot(endpoint_pricing_document((endpoint,)))

    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=workspace.id,
        key_hash=key.hash,
        estimate=300,
        has_credit_candidate=True,
        reservation_usage_type=UsageType.CREDITS,
        model_id=model.id,
        provider=endpoint.provider,
        requested_model_id=model.id,
        candidate_model_ids=[model.id],
        region="us-central1",
        endpoint_id=endpoint.id,
        candidate_endpoint_ids=[endpoint.id],
        idempotency_key="stage-d-idempotency",
        idempotency_fingerprint="f" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        pricing_snapshot=snapshot,
        stage_d_reason="ok",
        stage_d_prompt_tokens=100,
        stage_d_max_output_tokens=100,
        stage_d_boot_kid="boot-stage-d",
        invocation_nonce="invocation-original",
    )

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    row = db.gateway_authorizations[authorization.id]
    assert row["heartbeat_seq"] == 0
    assert row["pricing_snapshot"] == snapshot
    assert row["stage_d_boot_kid"] == "boot-stage-d"
    assert row["invocation_nonce"] == "invocation-original"
    assert [
        row["started_at"],
        row["heartbeat_at"],
        row["heartbeat_hash"],
        row["selected_endpoint_id"],
        row["delivered_usage"],
    ] == [None] * 5

    ineligible_outcome, ineligible = store.authorize_gateway_typed(
        workspace_id=workspace.id,
        key_hash=key.hash,
        estimate=300,
        has_credit_candidate=True,
        reservation_usage_type=UsageType.CREDITS,
        model_id=model.id,
        provider=endpoint.provider,
        requested_model_id=model.id,
        candidate_model_ids=[model.id],
        region="us-central1",
        endpoint_id=endpoint.id,
        candidate_endpoint_ids=[endpoint.id],
        idempotency_key="stage-d-ineligible",
        idempotency_fingerprint="e" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        stage_d_reason="not_streaming",
        stage_d_prompt_tokens=100,
        stage_d_max_output_tokens=100,
    )
    assert ineligible_outcome == AuthorizeOutcome.ACCEPTED
    assert ineligible is not None
    ineligible_row = db.gateway_authorizations[ineligible.id]
    assert [ineligible_row[column] for column in (
        "started_at",
        "heartbeat_seq",
        "heartbeat_at",
        "heartbeat_hash",
        "selected_endpoint_id",
        "delivered_usage",
        "pricing_snapshot",
    )] == [None] * 7


def test_stage_d_authorize_payload_uses_snapshot_and_lease_allocation_cap() -> None:
    document = json.loads((FIXTURES / "pricing_document.json").read_bytes())
    authorization = GatewayAuthorization(
        id="gwa-stage-d-eligible",
        workspace_id="workspace",
        key_hash="key",
        model_id="model",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=500,
        pricing_snapshot=canonical_pricing_snapshot(document),
        heartbeat_seq=0,
        stage_d_reason="ok",
        spend_lease_allocated_micro=300,
    )
    expected = json.loads((FIXTURES / "authorize_response_eligible.json").read_bytes())["data"]
    assert {
        "authorization_id": authorization.id,
        **_gateway_stage_d_payload(authorization),
    } == expected

    ineligible = GatewayAuthorization(
        id="gwa-stage-d-ineligible",
        workspace_id="workspace",
        key_hash="key",
        model_id="model",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=500,
        stage_d_reason="not_streaming",
    )
    expected_ineligible = json.loads(
        (FIXTURES / "authorize_response_ineligible.json").read_bytes()
    )["data"]
    assert {
        "authorization_id": ineligible.id,
        **_gateway_stage_d_payload(ineligible),
    } == expected_ineligible


def test_app_markup_and_receipt_fee_remain_in_stage_d_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _table = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="stage-d-fees", name="Stage D fees", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 1_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id,
        name="stage-d-fees",
        creator_user_id=workspace.owner_user_id,
        limit_microdollars=1_000_000,
    )
    configure_store(store)
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    monkeypatch.setattr(
        gateway,
        "_oauth_app_terms_for_key",
        lambda _key: (1_250, "app-owner"),
    )
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash,
        idempotency_key="stage-d-fees",
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100,
        max_output_tokens=100,
        stream=True,
        route_type="chat.completions",
        inference_receipt=True,
    )

    response = gateway._authorize_gateway_sync(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [(b"x-tr-boot-auth", b"kid=boot-fees,sig=x")],
            }
        ),
        body,
        Settings(
            environment="test",
            stage_d_eligibility_enabled=True,
            stage_d_pilot_workspace_ids=workspace.id,
        ),
    )["data"]

    stored = db.gateway_authorizations[response["authorization_id"]]
    assert json.loads(stored["payload"])["app_markup_basis_points"] == 1_250
    assert response["receipt_fee_basis_points"] == 1_200
    assert response["stage_d"] == {"eligible": True, "reason": "ok"}
    assert response["candidate_prices"]


def test_stage_d_eligibility_kill_switch_declares_nothing_eligible() -> None:
    """Emergency kill (2026-09-03): with eligibility off the router never puts a
    request in the Stage D cohort, so the enclave never sends a heartbeat."""
    from trusted_router.routes.internal.gateway import _stage_d_eligibility_reason

    assert Settings(environment="test").stage_d_eligibility_enabled is False
    rollout = (Path(__file__).parents[1] / "scripts" / "deploy" / "rollout.sh").read_text()
    for literal in (
        '"TR_STAGE_D_ELIGIBILITY_ENABLED=false"',
        '"TR_STAGE_D_HEARTBEAT_ENABLED=true"',
        '"TR_STAGE_D_PILOT_WORKSPACE_IDS=45819281-0ce9-4811-a0cd-c660ab3a116d"',
        '"TR_SPEND_LEASE_ACCEPTED_GCP_IMAGE_DIGESTS="',
        '"TR_REAP_SNAPSHOT_BOOKING_ENABLED=false"',
    ):
        assert literal in rollout
    reason = _stage_d_eligibility_reason(
        eligibility_enabled=False,
        stream=True,
        route_type="chat.completions",
        endpoint_candidates=[],
        standard_endpoint_pricing=True,
        service_tier=None,
        settlement_backend=True,
    )
    assert reason == "stage_d_disabled"


def test_stage_d_replay_is_always_ineligible_and_echoes_stored_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _table = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="stage-d-replay", name="Stage D replay", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 1_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id,
        name="stage-d-replay",
        creator_user_id=workspace.owner_user_id,
        limit_microdollars=1_000_000,
    )
    configure_store(store)
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash,
        idempotency_key="stage-d-replay",
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100,
        max_output_tokens=100,
        stream=True,
        route_type="chat.completions",
        invocation_nonce="original-invocation",
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-tr-boot-auth", b"kid=boot-replay,sig=x")],
        }
    )
    enabled = Settings(
        environment="test",
        stage_d_eligibility_enabled=True,
        stage_d_pilot_workspace_ids=workspace.id,
    )

    first = gateway._authorize_gateway_sync(request, body, enabled)["data"]
    assert first["stage_d"] == {"eligible": True, "reason": "ok"}
    assert "invocation_nonce" not in first
    stored = store.get_gateway_authorization(first["authorization_id"])
    assert stored is not None
    assert stored.invocation_nonce == "original-invocation"
    assert stored.stage_d_boot_kid == "boot-replay"

    replay_body = body.model_copy(update={"invocation_nonce": "different-invocation"})

    disabled_replay = gateway._authorize_gateway_sync(
        request,
        replay_body,
        Settings(environment="test", stage_d_eligibility_enabled=False),
    )["data"]
    assert disabled_replay["idempotent_replay"] is True
    assert disabled_replay["stage_d"] == {
        "eligible": False,
        "reason": "replayed",
    }
    assert disabled_replay["invocation_nonce"] == "original-invocation"
    assert "candidate_prices" not in disabled_replay
    assert "cap_micro" not in disabled_replay

    enabled_replay = gateway._authorize_gateway_sync(request, replay_body, enabled)["data"]
    assert enabled_replay["idempotent_replay"] is True
    assert enabled_replay["stage_d"] == {"eligible": False, "reason": "replayed"}
    assert enabled_replay["invocation_nonce"] == "original-invocation"
    assert "candidate_prices" not in enabled_replay
    assert "cap_micro" not in enabled_replay
