from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.spanner import make_fake_store
from tests.fakes.spend_lease_bigtable import FakeBigtableTable
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.config import Settings
from trusted_router.receipt_keys import b64url_decode, b64url_encode, receipt_kid
from trusted_router.routes.internal import gateway
from trusted_router.routing import normalize_routing_inputs
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.spend_lease_admission import (
    ADMISSION_REFUSAL_REASONS,
    AdmissionReceiptError,
    classify_receipt_replay,
    parse_admission_receipt,
    receipt_hash,
    verify_admission_receipt,
)
from trusted_router.spend_lease_ledger import BigtableSpendLeaseLedger
from trusted_router.spend_lease_state import SpendLease
from trusted_router.spend_leases import (
    SPEND_LEASE_COHORT,
    BootAuthHeader,
    SpendLeaseBoot,
    SpendLeaseSigner,
    boot_auth_digest,
    freeze_spend_lease_catalog,
    spend_lease_catalog_estimate,
)
from trusted_router.storage import CreditAccount, Workspace, configure_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_keys import _gateway_authorization_idempotency_index_id
from trusted_router.storage_gcp_spend_lease_authorize import SpendLeaseReuseLost


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _jwk(private: Ed25519PrivateKey) -> dict[str, str]:
    return {
        "crv": "Ed25519",
        "kty": "OKP",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }


def _receipt(private: Ed25519PrivateKey, claims: dict[str, Any]) -> str:
    protected = {
        "alg": "EdDSA",
        "kid": claims["boot_kid"],
        "typ": "spend_lease_admission+jws",
    }
    protected_segment = b64url_encode(_canonical(protected))
    payload_segment = b64url_encode(_canonical(claims))
    signing_input = f"{protected_segment}.{payload_segment}".encode()
    return (
        f"{protected_segment}.{payload_segment}."
        f"{b64url_encode(private.sign(signing_input))}"
    )


def _receipt_with_kid(
    private: Ed25519PrivateKey,
    claims: dict[str, Any],
    protected_kid: str,
) -> str:
    protected = {
        "alg": "EdDSA",
        "kid": protected_kid,
        "typ": "spend_lease_admission+jws",
    }
    protected_segment = b64url_encode(_canonical(protected))
    payload_segment = b64url_encode(_canonical(claims))
    signing_input = f"{protected_segment}.{payload_segment}".encode()
    return (
        f"{protected_segment}.{payload_segment}."
        f"{b64url_encode(private.sign(signing_input))}"
    )


def _request(raw: bytes, private: Ed25519PrivateKey, kid: str) -> Request:
    signature = private.sign(
        boot_auth_digest("POST", "/internal/gateway/authorize", raw)
    )
    header = f"kid={kid},sig={b64url_encode(signature)}".encode()
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/gateway/authorize",
            "headers": [(b"x-tr-boot-auth", header)],
        }
    )


def _admission_reason(exc: HTTPException) -> str:
    assert isinstance(exc.detail, dict)
    error = exc.detail.get("error")
    assert isinstance(error, dict)
    reason = error.get("reason")
    assert isinstance(reason, str)
    return reason


def _settings(workspace_id: str, digest: str) -> Settings:
    return Settings(
        environment="test",
        operational_analytics_sink="direct",
        spend_lease_issuance_enabled=True,
        spend_lease_binding_enabled=True,
        spend_lease_bigtable_app_profiles="us-central1=tr-spend-us-central1",
        spend_lease_admission_accept=True,
        spend_lease_pilot_workspace_ids=workspace_id,
        spend_lease_signing_secret_name="stage-c-test-seed",  # noqa: S106
        spend_lease_accepted_gcp_image_digests=digest,
    )


def _seed_store() -> tuple[Any, Any, Any, Ed25519PrivateKey, SpendLeaseBoot]:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="ws-stage-c", name="Stage C", owner_user_id="user-stage-c")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 50_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw_key, key = store.api_keys.create(
        workspace_id=workspace.id,
        name="stage-c",
        creator_user_id=workspace.owner_user_id,
    )
    store._spend_lease_ledger = BigtableSpendLeaseLedger(
        {"us-central1": FakeBigtableTable()}
    )
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    jwk = _jwk(private)
    digest = "sha256:" + "67" * 32
    boot = SpendLeaseBoot(
        kid=receipt_kid(jwk),
        jwk=jwk,
        approved=False,
        verified=True,
        image_digest=digest,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-09-03T00:00:00Z",
    )
    store.observe_spend_lease_boot(boot)
    configure_store(store)
    return store, database, key, private, boot


def _base_body(key: Any, idempotency_key: str) -> dict[str, Any]:
    return {
        "api_key_lookup_hash": key.lookup_hash,
        "estimated_input_tokens": 100,
        "idempotency_key": idempotency_key,
        "max_tokens": 100,
        "model": "anthropic/claude-haiku-4.5",
        "provider": {"allow_fallbacks": False, "usage": "credits"},
        "region": "us-central1",
        "route_type": "chat.completions",
    }


def _verification_harness() -> dict[str, Any]:
    store, _database, key, private, boot = _seed_store()
    settings = _settings(key.workspace_id, boot.image_digest)
    raw_body = _base_body(key, "verify-stage-c")
    normalized = normalize_routing_inputs(
        raw_body,
        settings,
        resolved_region="us-central1",
    )
    model = MODELS["anthropic/claude-haiku-4.5"]
    endpoint = next(
        candidate
        for candidate in endpoints_for_model(model.id)
        if candidate.usage_type == "Credits"
    )
    catalog = freeze_spend_lease_catalog(
        [(model, endpoint)],
        region="us-central1",
        route_type="chat.completions",
        service_tier=None,
        stage_c=True,
    )
    estimate = spend_lease_catalog_estimate(
        catalog,
        model=model.id,
        provider=None,
        route_type="chat.completions",
        region="us-central1",
        service_tier=None,
        estimated_input_tokens=100,
        max_tokens=100,
    )
    assert estimate is not None
    now = int(time.time())
    lease_id = "stage-c-verification-lease"
    lease_claims = {
        "authoritative": True,
        "boot_kid": boot.kid,
        "cap_micro": 10_000,
        "catalog": catalog,
        "cohort": SPEND_LEASE_COHORT,
        "exp": now + 60,
        "gen": 3,
        "iat": now - 1,
        "key_hash": key.hash,
        "lease_id": lease_id,
        "local_admission_allowed": True,
        "routing_policy_hash": normalized.routing_policy_hash,
        "typ": "spend-lease+jws",
        "v": 1,
        "workspace_id": key.workspace_id,
    }
    token = SpendLeaseSigner(lambda: bytes(range(32))).sign(lease_claims)
    lease_row = {
        "boot_kid": boot.kid,
        "cap_micro": 10_000,
        "catalog": catalog,
        "catalog_version": catalog["version"],
        "exp": now + 60,
        "gen": 3,
        "iat": now - 1,
        "issuer_kid": SpendLeaseSigner(lambda: bytes(range(32))).kid,
        "key_hash": key.hash,
        "lease_id": lease_id,
        "local_admission_allowed": True,
        "routing_policy_hash": normalized.routing_policy_hash,
        "state": "ACTIVE",
        "token": token,
        "workspace_id": key.workspace_id,
    }
    store._write_entity("spend_lease", lease_id, lease_row)
    receipt_claims = {
        "admitted_at_ms": now * 1000,
        "boot_kid": boot.kid,
        "enclave_estimate_micro": estimate,
        "gen": 3,
        "idempotency_key_sha256": hashlib.sha256(
            b"verify-stage-c"
        ).hexdigest(),
        "key_hash": key.hash,
        "lease_id": lease_id,
        "remaining_after_micro": 10_000 - estimate,
        "routing_policy_hash": normalized.routing_policy_hash,
        "v": 1,
        "workspace_id": key.workspace_id,
    }
    return {
        "boot": boot,
        "context": {
            "boot_auth": BootAuthHeader(boot.kid, "verified-by-route"),
            "boot_verified": True,
        },
        "key": key,
        "lease_claims": lease_claims,
        "lease_row": lease_row,
        "normalized": normalized,
        "private": private,
        "raw_body": raw_body,
        "receipt_claims": receipt_claims,
        "settings": settings,
        "store": store,
    }


def _verify_harness(harness: dict[str, Any]) -> Any:
    body_dict = dict(harness["raw_body"])
    body_dict["spend_lease_admission"] = harness.get(
        "receipt",
        _receipt(harness["private"], harness["receipt_claims"]),
    )
    return gateway._verify_gateway_spend_lease_admission(  # noqa: SLF001
        body=GatewayAuthorizeRequest(**body_dict),
        settings=harness["settings"],
        spend_context=harness["context"],
        workspace_id=harness["key"].workspace_id,
        key_hash=harness["key"].hash,
        idempotency_key="verify-stage-c",
        normalized_routing=harness["normalized"],
        cohort_eligible=True,
    )


def test_closed_refusal_set_is_exact() -> None:
    assert ADMISSION_REFUSAL_REASONS == {
        "receipt_invalid",
        "boot_not_accepted",
        "boot_mismatch",
        "lease_not_open",
        "window",
        "policy_mismatch",
        "estimate_mismatch",
        "capacity",
        "hold_refused",
        "scope_conflict",
        "reuse_lost",
        "not_accepting",
    }


@pytest.mark.parametrize(
    ("incoming", "stored", "expected"),
    [
        ("a", "a", "replay"),
        ("a", "b", "scope_conflict"),
        ("a", None, "scope_conflict"),
        (None, "a", "scope_conflict"),
        (None, None, "ordinary"),
    ],
)
def test_decision_60_receipt_replay_truth_table(
    incoming: str | None,
    stored: str | None,
    expected: str,
) -> None:
    assert classify_receipt_replay(incoming, stored) == expected


def test_receipt_parser_rejects_embedded_jwk_and_noncanonical_shape() -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    claims = {
        "admitted_at_ms": 1,
        "boot_kid": "boot",
        "enclave_estimate_micro": 1,
        "gen": 1,
        "idempotency_key_sha256": "1" * 64,
        "key_hash": "key",
        "lease_id": "lease",
        "remaining_after_micro": 0,
        "routing_policy_hash": "2" * 64,
        "v": 1,
        "workspace_id": "workspace",
    }
    compact = _receipt(private, claims)
    protected, payload, signature = compact.split(".")
    header = json.loads(b64url_decode(protected))
    header["jwk"] = _jwk(private)
    embedded = f"{b64url_encode(_canonical(header))}.{payload}.{signature}"

    with pytest.raises(AdmissionReceiptError):
        parse_admission_receipt(embedded)
    claims["unknown"] = True
    with pytest.raises(AdmissionReceiptError):
        parse_admission_receipt(_receipt(private, claims))


def test_receipt_verification_ignores_historical_approved_bit() -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    jwk = _jwk(private)
    kid = receipt_kid(jwk)
    claims = {
        "admitted_at_ms": 1,
        "boot_kid": kid,
        "enclave_estimate_micro": 1,
        "gen": 1,
        "idempotency_key_sha256": "1" * 64,
        "key_hash": "key",
        "lease_id": "lease",
        "remaining_after_micro": 0,
        "routing_policy_hash": "2" * 64,
        "v": 1,
        "workspace_id": "workspace",
    }
    boot = SpendLeaseBoot(
        kid=kid,
        jwk=jwk,
        approved=False,
        verified=True,
        image_digest="digest",
        attestation_kind="gcp-cs-jwt",
        registered_at="now",
    )

    assert verify_admission_receipt(_receipt(private, claims), boot).claims.gen == 1


def test_receipt_verifier_checks_protected_boot_ownership_before_signature() -> None:
    private_a = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    private_b = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    jwk_a = _jwk(private_a)
    kid_a = receipt_kid(jwk_a)
    kid_b = receipt_kid(_jwk(private_b))
    claims = {
        "admitted_at_ms": 1,
        "boot_kid": kid_b,
        "enclave_estimate_micro": 1,
        "gen": 1,
        "idempotency_key_sha256": "1" * 64,
        "key_hash": "key",
        "lease_id": "lease",
        "remaining_after_micro": 0,
        "routing_policy_hash": "2" * 64,
        "v": 1,
        "workspace_id": "workspace",
    }
    boot_a = SpendLeaseBoot(
        kid=kid_a,
        jwk=jwk_a,
        approved=False,
        verified=True,
        image_digest="digest",
        attestation_kind="gcp-cs-jwt",
        registered_at="now",
    )

    with pytest.raises(
        AdmissionReceiptError,
        match="^receipt boot ownership mismatch$",
    ):
        verify_admission_receipt(
            _receipt_with_kid(private_a, claims, kid_a),
            boot_a,
        )


def test_receipt_verifier_rejects_a_different_registered_boot_before_signature() -> None:
    private_b = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    jwk_b = _jwk(private_b)
    kid_b = receipt_kid(jwk_b)
    claims = {
        "admitted_at_ms": 1,
        "boot_kid": kid_b,
        "enclave_estimate_micro": 1,
        "gen": 1,
        "idempotency_key_sha256": "1" * 64,
        "key_hash": "key",
        "lease_id": "lease",
        "remaining_after_micro": 0,
        "routing_policy_hash": "2" * 64,
        "v": 1,
        "workspace_id": "workspace",
    }
    registered_boot_a = SpendLeaseBoot(
        kid="registered-boot-a",
        jwk=jwk_b,
        approved=False,
        verified=True,
        image_digest="digest",
        attestation_kind="gcp-cs-jwt",
        registered_at="now",
    )

    with pytest.raises(
        AdmissionReceiptError,
        match="^receipt boot is unavailable$",
    ):
        verify_admission_receipt(_receipt(private_b, claims), registered_boot_a)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("protected_payload_kid", "boot_mismatch"),
        ("signature", "receipt_invalid"),
        ("boot_unverified", "boot_not_accepted"),
        ("image_not_allowed", "boot_not_accepted"),
        ("non_gcp", "boot_not_accepted"),
        ("workspace", "receipt_invalid"),
        ("key", "receipt_invalid"),
        ("idempotency", "receipt_invalid"),
        ("closed", "lease_not_open"),
        ("lease_row_scope", "lease_not_open"),
        ("token_mismatch", "lease_not_open"),
        ("window", "window"),
        ("policy", "policy_mismatch"),
        ("boot_auth_signature", "boot_not_accepted"),
        ("boot_auth_kid", "boot_mismatch"),
        ("registered_boot_kid", "boot_mismatch"),
        ("lease_row_boot", "boot_mismatch"),
        ("lease_token_boot", "boot_mismatch"),
    ],
)
def test_decision_59_verification_matrix(
    case: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _verification_harness()
    store = harness["store"]
    boot = harness["boot"]
    if case == "protected_payload_kid":
        harness["receipt"] = _receipt_with_kid(
            harness["private"],
            harness["receipt_claims"],
            "different-boot",
        )
    elif case == "signature":
        compact = _receipt(harness["private"], harness["receipt_claims"])
        protected, payload, signature = compact.split(".")
        signature = f"{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
        harness["receipt"] = f"{protected}.{payload}.{signature}"
    elif case == "boot_unverified":
        store._write_entity(
            "spend_lease_boot",
            boot.kid,
            replace(boot, verified=False),
        )
    elif case == "image_not_allowed":
        harness["settings"] = harness["settings"].model_copy(
            update={"spend_lease_accepted_gcp_image_digests": "other"}
        )
    elif case == "non_gcp":
        store._write_entity(
            "spend_lease_boot",
            boot.kid,
            replace(boot, attestation_kind="aws-nitro"),
        )
    elif case == "workspace":
        harness["receipt_claims"]["workspace_id"] = "different-workspace"
    elif case == "key":
        harness["receipt_claims"]["key_hash"] = "different-key"
    elif case == "idempotency":
        harness["receipt_claims"]["idempotency_key_sha256"] = "0" * 64
    elif case == "closed":
        harness["lease_row"]["state"] = "CLOSED"
        store._write_entity(
            "spend_lease", harness["lease_row"]["lease_id"], harness["lease_row"]
        )
    elif case == "lease_row_scope":
        harness["lease_row"]["workspace_id"] = "different-workspace"
        store._write_entity(
            "spend_lease", harness["lease_row"]["lease_id"], harness["lease_row"]
        )
    elif case == "token_mismatch":
        harness["lease_claims"]["key_hash"] = "different-key"
        harness["lease_row"]["token"] = SpendLeaseSigner(
            lambda: bytes(range(32))
        ).sign(harness["lease_claims"])
        store._write_entity(
            "spend_lease", harness["lease_row"]["lease_id"], harness["lease_row"]
        )
    elif case == "window":
        harness["receipt_claims"]["admitted_at_ms"] = (
            harness["lease_claims"]["exp"] + 1
        ) * 1000
    elif case == "policy":
        harness["receipt_claims"]["routing_policy_hash"] = "0" * 64
    elif case == "boot_auth_signature":
        harness["context"]["boot_verified"] = False
    elif case == "boot_auth_kid":
        harness["context"]["boot_auth"] = BootAuthHeader(
            "different-boot", "signature"
        )
    elif case == "registered_boot_kid":
        registered_boot = replace(boot, kid="different-registered-boot")
        monkeypatch.setattr(
            type(store),
            "get_spend_lease_boot",
            lambda _store, _kid: registered_boot,
        )
    elif case == "lease_row_boot":
        harness["lease_row"]["boot_kid"] = "different-boot"
        store._write_entity(
            "spend_lease", harness["lease_row"]["lease_id"], harness["lease_row"]
        )
    elif case == "lease_token_boot":
        harness["lease_claims"]["boot_kid"] = "different-boot"
        harness["lease_row"]["token"] = SpendLeaseSigner(
            lambda: bytes(range(32))
        ).sign(harness["lease_claims"])
        store._write_entity(
            "spend_lease", harness["lease_row"]["lease_id"], harness["lease_row"]
        )
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    with pytest.raises(HTTPException) as raised:
        _verify_harness(harness)
    assert _admission_reason(raised.value) == reason


def test_verifier_accepts_draining_lease_and_rejects_after_flag_gate() -> None:
    harness = _verification_harness()
    harness["lease_row"]["state"] = "DRAINING"
    harness["store"]._write_entity(
        "spend_lease", harness["lease_row"]["lease_id"], harness["lease_row"]
    )
    artifact, _hash, _estimate, _candidates = _verify_harness(harness)
    assert artifact.lease_status == "draining"

    harness["settings"] = harness["settings"].model_copy(
        update={"spend_lease_admission_accept": False}
    )
    with pytest.raises(HTTPException) as raised:
        _verify_harness(harness)
    assert _admission_reason(raised.value) == "not_accepting"


def test_stage_c_mint_and_direct_presented_lease_reuse_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key, private, boot = _seed_store()
    settings = _settings(key.workspace_id, boot.image_digest)
    monkeypatch.setattr(
        gateway,
        "_spend_lease_signer",
        lambda _settings: SpendLeaseSigner(lambda: bytes(range(32))),
    )
    monkeypatch.setattr(gateway, "_record_spend_lease_shadow", lambda *_a, **_kw: None)

    mint_body = _base_body(key, "stage-c-mint")
    mint_raw = _canonical(mint_body)
    minted = gateway._authorize_gateway_sync(
        _request(mint_raw, private, boot.kid),
        GatewayAuthorizeRequest(**mint_body),
        settings,
        mint_raw,
    )
    assert "spend_lease" in minted["data"], minted
    lease_token = minted["data"]["spend_lease"]["token"]
    lease_claims = json.loads(b64url_decode(lease_token.split(".")[1]))
    assert lease_claims["cohort"] == SPEND_LEASE_COHORT
    assert lease_claims["local_admission_allowed"] is True
    assert lease_claims["routing_policy_hash"]
    assert set(lease_claims["catalog"]["candidates"][0]) >= {
        "upstream_model",
        "usage_type",
        "wafer_zdr_required",
    }

    reserve_body = _base_body(key, "stage-c-reserve")
    normalized = normalize_routing_inputs(
        reserve_body,
        settings,
        resolved_region="us-central1",
    )
    assert normalized.routing_policy_hash == lease_claims["routing_policy_hash"]
    estimate = minted["data"]["estimated_cost_microdollars"]
    receipt_claims = {
        "admitted_at_ms": int(time.time() * 1000),
        "boot_kid": boot.kid,
        "enclave_estimate_micro": estimate,
        "gen": lease_claims["gen"],
        "idempotency_key_sha256": hashlib.sha256(
            b"stage-c-reserve"
        ).hexdigest(),
        "key_hash": key.hash,
        "lease_id": lease_claims["lease_id"],
        "remaining_after_micro": lease_claims["cap_micro"] - 2 * estimate,
        "routing_policy_hash": normalized.routing_policy_hash,
        "v": 1,
        "workspace_id": key.workspace_id,
    }
    compact = _receipt(private, receipt_claims)
    reserve_body["spend_lease_admission"] = compact
    reserve_raw = _canonical(reserve_body)
    reserved_before = database.typed[CREDIT_BALANCE_TABLE][
        (key.workspace_id, 0)
    ]["reserved"]

    accepted = gateway._authorize_gateway_sync(
        _request(reserve_raw, private, boot.kid),
        GatewayAuthorizeRequest(**reserve_body),
        settings,
        reserve_raw,
    )

    data = accepted["data"]
    assert data["estimated_cost_microdollars"] == estimate
    assert data["spend_lease_admission"] == {
        "accepted": True,
        "receipt_hash": receipt_hash(compact),
    }
    assert data["spend_lease"]["remaining_micro"] == (
        lease_claims["cap_micro"] - 2 * estimate
    )
    assert data["route_candidates"][0]["upstream_model"] == (
        lease_claims["catalog"]["candidates"][0]["upstream_model"]
    )
    assert database.typed[CREDIT_BALANCE_TABLE][(key.workspace_id, 0)][
        "reserved"
    ] == reserved_before
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None
    assert authorization.receipt_fee_basis_points == 0
    assert authorization.settlement == "spend_lease"
    assert authorization.spend_lease_admission_receipt == compact
    assert authorization.spend_lease_receipt_hash == receipt_hash(compact)

    replay = gateway._authorize_gateway_sync(
        _request(reserve_raw, private, boot.kid),
        GatewayAuthorizeRequest(**reserve_body),
        settings,
        reserve_raw,
    )
    assert replay["data"]["idempotent_replay"] is True
    assert replay["data"]["authorization_id"] == data["authorization_id"]
    assert replay["data"]["spend_lease"]["remaining_micro"] == data[
        "spend_lease"
    ]["remaining_micro"]

    changed_claims = dict(
        receipt_claims,
        remaining_after_micro=receipt_claims["remaining_after_micro"] - 1,
    )
    changed_body = dict(
        reserve_body,
        spend_lease_admission=_receipt(private, changed_claims),
    )
    changed_raw = _canonical(changed_body)
    with pytest.raises(HTTPException) as changed_replay:
        gateway._authorize_gateway_sync(
            _request(changed_raw, private, boot.kid),
            GatewayAuthorizeRequest(**changed_body),
            settings,
            changed_raw,
        )
    assert _admission_reason(changed_replay.value) == "scope_conflict"

    receiptless_body = _base_body(key, "stage-c-reserve")
    receiptless_raw = _canonical(receiptless_body)
    with pytest.raises(HTTPException) as missing_receipt:
        gateway._authorize_gateway_sync(
            _request(receiptless_raw, private, boot.kid),
            GatewayAuthorizeRequest(**receiptless_body),
            settings,
            receiptless_raw,
        )
    assert _admission_reason(missing_receipt.value) == "scope_conflict"

    mint_scope_claims = dict(
        receipt_claims,
        idempotency_key_sha256=hashlib.sha256(b"stage-c-mint").hexdigest(),
    )
    mint_receipt_body = dict(
        mint_body,
        spend_lease_admission=_receipt(private, mint_scope_claims),
    )
    mint_receipt_raw = _canonical(mint_receipt_body)
    with pytest.raises(HTTPException) as stored_null:
        gateway._authorize_gateway_sync(
            _request(mint_receipt_raw, private, boot.kid),
            GatewayAuthorizeRequest(**mint_receipt_body),
            settings,
            mint_receipt_raw,
        )
    assert _admission_reason(stored_null.value) == "scope_conflict"

    ordinary_replay = gateway._authorize_gateway_sync(
        _request(mint_raw, private, boot.kid),
        GatewayAuthorizeRequest(**mint_body),
        settings,
        mint_raw,
    )
    assert ordinary_replay["data"]["idempotent_replay"] is True

    fee_idempotency_key = "stage-c-receipt-fee"
    fee_claims = dict(
        receipt_claims,
        idempotency_key_sha256=hashlib.sha256(
            fee_idempotency_key.encode()
        ).hexdigest(),
        remaining_after_micro=receipt_claims["remaining_after_micro"] - estimate,
    )
    fee_body = {
        **_base_body(key, fee_idempotency_key),
        "inference_receipt": True,
        "spend_lease_admission": _receipt(private, fee_claims),
    }
    fee_raw = _canonical(fee_body)
    with pytest.raises(HTTPException) as nonzero_receipt_fee:
        gateway._authorize_gateway_sync(
            _request(fee_raw, private, boot.kid),
            GatewayAuthorizeRequest(**fee_body),
            settings,
            fee_raw,
        )
    assert _admission_reason(nonzero_receipt_fee.value) == "policy_mismatch"


def test_existing_local_admission_refuses_without_fallthrough() -> None:
    harness = _verification_harness()
    store = harness["store"]
    lease_row = harness["lease_row"]
    artifact = store.get_spend_lease_for_admission(
        lease_row["lease_id"],
        harness["key"].workspace_id,
        harness["key"].hash,
    )
    assert artifact is not None
    ledger = store._spend_lease_ledger
    assert ledger is not None
    expires_at = datetime.fromtimestamp(artifact.exp, tz=UTC)
    ledger.initialize(
        SpendLease(
            lease_id=artifact.lease_id,
            gen=artifact.gen,
            key_hash=harness["key"].hash,
            boot_kid=artifact.boot_kid,
            workspace_id=harness["key"].workspace_id,
            creating_authorization_id="creating-authorization",
            cap_micro=artifact.cap_micro,
            expires_at=expires_at,
            skew=timedelta(seconds=10),
            version=0,
        ),
        region="us-central1",
    )
    scope = _gateway_authorization_idempotency_index_id(
        harness["key"].workspace_id,
        harness["key"].hash,
        "existing-local",
    )
    ledger.allocate(
        None,
        artifact.lease_id,
        region="us-central1",
        idempotency_scope=scope,
        provisional_authorization_id="foreign-provisional",
        request_fingerprint="fingerprint",
        allocated_micro=100,
        abandon_after=expires_at + timedelta(seconds=10),
        now=datetime.now(UTC),
        admission_receipt=True,
    )

    plan, reason, replay = store.prepare_gateway_spend_lease_admission(
        artifact=artifact,
        workspace_id=harness["key"].workspace_id,
        key_hash=harness["key"].hash,
        authorization_id="our-provisional",
        idempotency_key="existing-local",
        idempotency_fingerprint="fingerprint",
        estimate=100,
        region="us-central1",
        receipt_hash="a" * 64,
        skew_seconds=10,
    )

    assert (plan, reason, replay) == (None, "reuse_lost", None)


def test_reuse_lost_admission_refuses_without_ordinary_authorization() -> None:
    store, database, key, _private, _boot = _seed_store()
    artifact_harness = _verification_harness()
    artifact = artifact_harness["store"].get_spend_lease_for_admission(
        "stage-c-verification-lease",
        artifact_harness["key"].workspace_id,
        artifact_harness["key"].hash,
    )
    assert artifact is not None

    class LostPlan:
        provisional_id = "lost-authorization"
        allocation_micro = 100

        def __init__(self, lease_artifact: Any) -> None:
            self.artifact = lease_artifact

        def transaction_hook(self, *_args: Any) -> dict[str, Any]:
            raise SpendLeaseReuseLost("lease_transferred")

    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        authorization_id="lost-authorization",
        estimate=100,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="anthropic/claude-haiku-4.5",
        provider="anthropic",
        requested_model_id="anthropic/claude-haiku-4.5",
        candidate_model_ids=["anthropic/claude-haiku-4.5"],
        region="us-central1",
        endpoint_id="anthropic/claude-haiku-4.5@anthropic/prepaid",
        candidate_endpoint_ids=[
            "anthropic/claude-haiku-4.5@anthropic/prepaid"
        ],
        idempotency_key="reuse-lost",
        idempotency_fingerprint="fingerprint",
        spend_lease_binding_plan=LostPlan(artifact),
        spend_lease_admission_receipt="receipt",
        spend_lease_receipt_hash="a" * 64,
        credit_escrowed_by_spend_lease=True,
        spend_lease_admission_replay_protection=True,
    )

    assert outcome == "admission_rejected:reuse_lost"
    assert authorization is None
    assert database.reservations == {}


def test_receipt_validation_runs_while_acceptance_flag_is_off() -> None:
    body = GatewayAuthorizeRequest(
        api_key_hash="key",
        idempotency_key="idem",
        model="anthropic/claude-haiku-4.5",
        spend_lease_admission="not-a-compact-jws",
    )
    normalized = normalize_routing_inputs(
        body.model_dump(exclude_none=True),
        Settings(environment="test"),
    )

    with pytest.raises(HTTPException) as raised:
        gateway._verify_gateway_spend_lease_admission(  # noqa: SLF001
            body=body,
            settings=Settings(environment="test"),
            spend_context={"boot_auth": None, "boot_verified": False},
            workspace_id="workspace",
            key_hash="key",
            idempotency_key="idem",
            normalized_routing=normalized,
            cohort_eligible=True,
        )
    assert _admission_reason(raised.value) == "receipt_invalid"


def test_admission_flag_requires_binding_and_defaults_off() -> None:
    assert Settings(environment="test").spend_lease_admission_accept is False
    with pytest.raises(ValueError, match="TR_SPEND_LEASE_ADMISSION_ACCEPT"):
        Settings(environment="test", spend_lease_admission_accept=True)
