from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.receipt_keys import b64url_decode, b64url_encode, receipt_kid
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest, SpendLeaseBootRegistrationRequest
from trusted_router.spend_leases import (
    SpendLeaseBoot,
    SpendLeaseSigner,
    boot_auth_digest,
)
from trusted_router.storage import STORE


def _wait_for_shadow_delivery() -> None:
    assert gateway._SPEND_LEASE_SHADOW_DISPATCHER.wait_for_idle(1)  # noqa: SLF001


@pytest.fixture(autouse=True)
def _isolate_shadow_delivery() -> Any:
    _wait_for_shadow_delivery()
    yield
    _wait_for_shadow_delivery()


def _request(
    path: str = "/v1/internal/gateway/authorize",
    boot_auth_header: str | None = None,
) -> Request:
    headers = (
        [(b"x-tr-boot-auth", boot_auth_header.encode())]
        if boot_auth_header is not None
        else []
    )
    return Request({"type": "http", "method": "POST", "path": path, "headers": headers})


def _jwk(private: Ed25519PrivateKey) -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }


def _registration_settings(digest: str) -> Settings:
    return Settings(
        environment="test",
        spend_lease_accepted_gcp_image_digests=digest,
    )


def test_boot_registration_accepts_verified_gcp_approved_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    digest = "sha256:" + "11" * 32
    private = Ed25519PrivateKey.generate()
    jwk = _jwk(private)
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    monkeypatch.setattr(gateway, "verify_gcp_attestation_chain", lambda _att: None)
    monkeypatch.setattr(gateway, "gcp_attestation_image_digest", lambda _att: digest)
    response = gateway._register_spend_lease_boot_sync(  # noqa: SLF001
        _request("/v1/internal/gateway/spend-lease/register-boot"),
        SpendLeaseBootRegistrationRequest(
            kid=receipt_kid(jwk),
            receipt_public_key=jwk,
            attestation_evidence="signed-gcp-evidence",
            attestation_kind="gcp",
        ),
        _registration_settings(digest),
    )
    assert response == {"data": {"verified": True}}


def test_boot_registration_rejects_wrong_gcp_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    configured = "sha256:" + "11" * 32
    observed = "sha256:" + "22" * 32
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    monkeypatch.setattr(gateway, "verify_gcp_attestation_chain", lambda _att: None)
    monkeypatch.setattr(gateway, "gcp_attestation_image_digest", lambda _att: observed)
    response = gateway._register_spend_lease_boot_sync(  # noqa: SLF001
        _request("/v1/internal/gateway/spend-lease/register-boot"),
        SpendLeaseBootRegistrationRequest(
            kid=receipt_kid(jwk),
            receipt_public_key=jwk,
            attestation_evidence="signed-gcp-evidence",
            attestation_kind="gcp",
        ),
        _registration_settings(configured),
    )
    assert response == {"data": {"verified": True}}
    assert STORE.get_spend_lease_boot(receipt_kid(jwk)).approved is False  # type: ignore[union-attr]


def test_boot_registration_rejects_bad_gcp_chain_without_storing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)

    def bad_chain(_att: str) -> None:
        raise ValueError("bad chain")

    monkeypatch.setattr(gateway, "verify_gcp_attestation_chain", bad_chain)
    with pytest.raises(HTTPException, match="bad chain"):
        gateway._register_spend_lease_boot_sync(  # noqa: SLF001
            _request("/v1/internal/gateway/spend-lease/register-boot"),
            SpendLeaseBootRegistrationRequest(
                kid=receipt_kid(jwk),
                receipt_public_key=jwk,
                attestation_evidence="forged",
                attestation_kind="gcp",
            ),
            _registration_settings("sha256:" + "11" * 32),
        )
    assert STORE.get_spend_lease_boot(receipt_kid(jwk)) is None


def test_boot_registration_records_aws_as_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    response = gateway._register_spend_lease_boot_sync(  # noqa: SLF001
        _request("/v1/internal/gateway/spend-lease/register-boot"),
        SpendLeaseBootRegistrationRequest(
            kid=receipt_kid(jwk),
            receipt_public_key=jwk,
            attestation_evidence="bound-aws-cose",
            attestation_kind="aws",
        ),
        _registration_settings("sha256:" + "11" * 32),
    )
    assert response == {"data": {"verified": False}}
    assert STORE.get_spend_lease_boot(receipt_kid(jwk)).approved is False  # type: ignore[union-attr]


def test_boot_registration_wire_contract_accepts_literal_enclave_body(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire_fixture = (
        '{"kid":"testkid","receipt_public_key":{"kty":"OKP","crv":"Ed25519",'
        '"x":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},'
        '"attestation_evidence":"<...>","attestation_kind":"gcp"}'
    )
    monkeypatch.setattr(gateway, "receipt_kid", lambda _jwk: "testkid")
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    monkeypatch.setattr(gateway, "verify_gcp_attestation_chain", lambda _att: None)
    monkeypatch.setattr(gateway, "gcp_attestation_image_digest", lambda _att: "")

    response = client.post(
        "/internal/gateway/spend-lease/register-boot",
        content=wire_fixture,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"data"}
    assert set(payload["data"]) == {"verified"}
    assert isinstance(payload["data"]["verified"], bool)


def _seed_authorize() -> tuple[Any, Any, Ed25519PrivateKey, SpendLeaseBoot]:
    STORE.reset()
    user = STORE.ensure_user("spend-lease@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(workspace.id, 20_000_000, "seed")
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="spend lease",
        creator_user_id=user.id,
    )
    private = Ed25519PrivateKey.generate()
    jwk = _jwk(private)
    boot = SpendLeaseBoot(
        kid=receipt_kid(jwk),
        jwk=jwk,
        approved=True,
        verified=True,
        image_digest="sha256:" + "11" * 32,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-08-27T00:00:00Z",
    )
    STORE.observe_spend_lease_boot(boot)
    return workspace, key, private, boot


def _signed_authorize_body(
    key: Any,
    private: Ed25519PrivateKey,
    boot: SpendLeaseBoot,
    *,
    idempotency_key: str = "spend-lease-replay",
    echo: dict[str, Any] | None = None,
    route_type: str | None = "chat.completions",
) -> tuple[dict[str, Any], bytes, str]:
    body: dict[str, Any] = {
        "api_key_lookup_hash": key.lookup_hash,
        "idempotency_key": idempotency_key,
        "model": "anthropic/claude-haiku-4.5",
        "estimated_input_tokens": 100,
        "max_tokens": 100,
        "route_type": route_type,
        "spend_lease_echo": echo
        or {
            "lease_id": None,
            "state": "empty",
            "remaining_micro": None,
            "enclave_estimate_micro": 1_000_000,
            "catalog_version": None,
            "would_admit": False,
        },
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = private.sign(
        boot_auth_digest("POST", "/v1/internal/gateway/authorize", raw_body)
    )
    header = f"kid={boot.kid},sig={b64url_encode(signature)}"
    return body, raw_body, header


def test_authorize_mutation_guard_uses_current_digest_not_persisted_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    user = STORE.ensure_user("spend-lease-current-approval@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(workspace.id, 20_000_000, "seed")
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="spend lease current approval",
        creator_user_id=user.id,
    )
    old_digest = "sha256:" + "11" * 32
    boot_digest = "sha256:" + "22" * 32
    private = Ed25519PrivateKey.generate()
    jwk = _jwk(private)
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    monkeypatch.setattr(gateway, "verify_gcp_attestation_chain", lambda _att: None)
    monkeypatch.setattr(gateway, "gcp_attestation_image_digest", lambda _att: boot_digest)

    gateway._register_spend_lease_boot_sync(  # noqa: SLF001
        _request("/v1/internal/gateway/spend-lease/register-boot"),
        SpendLeaseBootRegistrationRequest(
            kid=receipt_kid(jwk),
            receipt_public_key=jwk,
            attestation_evidence="signed-gcp-evidence",
            attestation_kind="gcp",
        ),
        _registration_settings(old_digest),
    )
    boot = STORE.get_spend_lease_boot(receipt_kid(jwk))
    assert boot is not None
    assert boot.verified is True
    assert boot.approved is False

    monkeypatch.setattr(
        gateway,
        "_spend_lease_signer",
        lambda _settings: SpendLeaseSigner(lambda: bytes(range(32))),
    )
    body_dict, raw_body, header = _signed_authorize_body(key, private, boot)
    current_settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_pilot_workspace_ids=workspace.id,
        spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
        trust_gcp_image_digest=boot_digest,
    )

    authorized = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=header),
        GatewayAuthorizeRequest(**body_dict),
        current_settings,
        raw_body,
    )

    assert "spend_lease" in authorized["data"]
    assert STORE.get_spend_lease_boot(boot.kid) is boot
    assert boot.approved is False


def test_authorize_mints_shadow_grant_and_replay_returns_byte_identical_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, key, private, boot = _seed_authorize()
    signer = SpendLeaseSigner(lambda: bytes(range(32)))
    monkeypatch.setattr(gateway, "_spend_lease_signer", lambda _settings: signer)
    body_dict, raw_body, header = _signed_authorize_body(key, private, boot)
    body = GatewayAuthorizeRequest(**body_dict)
    settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_pilot_workspace_ids=workspace.id,
        spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
        spend_lease_accepted_gcp_image_digests=boot.image_digest,
    )
    first = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=header), body, settings, raw_body
    )
    replay = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=header), body, settings, raw_body
    )
    first_lease = first["data"]["spend_lease"]
    replay_lease = replay["data"]["spend_lease"]
    assert first_lease["token"].encode() == replay_lease["token"].encode()
    assert first_lease["lease_status"] == replay_lease["lease_status"] == "active"
    assert first["data"]["idempotent_replay"] is False
    assert replay["data"]["idempotent_replay"] is True
    authorization = STORE.get_gateway_authorization(first["data"]["authorization_id"])
    assert authorization is not None
    assert authorization.spend_lease_token == first_lease["token"]
    assert authorization.spend_lease_status == "active"


def test_authorize_retains_one_active_grant_until_exhaustion_then_increases_gen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, key, private, boot = _seed_authorize()
    monkeypatch.setattr(
        gateway,
        "_spend_lease_signer",
        lambda _settings: SpendLeaseSigner(lambda: bytes(range(32))),
    )
    settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_pilot_workspace_ids=workspace.id,
        spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
        spend_lease_accepted_gcp_image_digests=boot.image_digest,
    )

    first_dict, first_raw, first_header = _signed_authorize_body(
        key, private, boot, idempotency_key="active-grant-1"
    )
    first = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=first_header),
        GatewayAuthorizeRequest(**first_dict),
        settings,
        first_raw,
    )["data"]["spend_lease"]["token"]
    first_claims = json.loads(b64url_decode(first.split(".")[1]))
    active_echo = {
        "lease_id": first_claims["lease_id"],
        "state": "active",
        "remaining_micro": first_claims["cap_micro"],
        "enclave_estimate_micro": 1_000_000,
        "catalog_version": first_claims["catalog"]["version"],
        "would_admit": True,
    }
    second_dict, second_raw, second_header = _signed_authorize_body(
        key,
        private,
        boot,
        idempotency_key="active-grant-2",
        echo=active_echo,
    )
    second = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=second_header),
        GatewayAuthorizeRequest(**second_dict),
        settings,
        second_raw,
    )["data"]["spend_lease"]["token"]
    assert second == first

    exhausted_echo = dict(active_echo, state="exhausted", remaining_micro=0)
    third_dict, third_raw, third_header = _signed_authorize_body(
        key,
        private,
        boot,
        idempotency_key="active-grant-3",
        echo=exhausted_echo,
    )
    third = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=third_header),
        GatewayAuthorizeRequest(**third_dict),
        settings,
        third_raw,
    )["data"]["spend_lease"]["token"]
    third_claims = json.loads(b64url_decode(third.split(".")[1]))
    assert third != first
    assert third_claims["gen"] > first_claims["gen"]


def test_authorize_shadow_records_reason_without_lease_and_null_when_minted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, key, private, boot = _seed_authorize()
    monkeypatch.setattr(
        gateway,
        "_spend_lease_signer",
        lambda _settings: SpendLeaseSigner(lambda: bytes(range(32))),
    )
    settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_pilot_workspace_ids=workspace.id,
        spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
        spend_lease_accepted_gcp_image_digests=boot.image_digest,
    )
    rejected_dict, rejected_raw, rejected_header = _signed_authorize_body(
        key,
        private,
        boot,
        idempotency_key="shadow-reason-rejected",
        route_type=None,
    )
    rejected = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=rejected_header),
        GatewayAuthorizeRequest(**rejected_dict),
        settings,
        rejected_raw,
    )
    minted_dict, minted_raw, minted_header = _signed_authorize_body(
        key,
        private,
        boot,
        idempotency_key="shadow-reason-minted",
    )
    minted = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=minted_header),
        GatewayAuthorizeRequest(**minted_dict),
        settings,
        minted_raw,
    )
    _wait_for_shadow_delivery()

    assert "spend_lease" not in rejected["data"]
    assert "spend_lease" in minted["data"]
    rejected_event = STORE.spend_lease_shadow_events[rejected["data"]["authorization_id"]]
    minted_event = STORE.spend_lease_shadow_events[minted["data"]["authorization_id"]]
    assert rejected_event["no_lease_reason"] == "route_type"
    assert minted_event["no_lease_reason"] is None


def test_authorize_shadow_names_current_boot_digest_approval_failure() -> None:
    workspace, key, private, boot = _seed_authorize()
    body_dict, raw_body, header = _signed_authorize_body(
        key,
        private,
        boot,
        idempotency_key="shadow-boot-digest-not-accepted",
    )
    settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_pilot_workspace_ids=workspace.id,
        spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
        spend_lease_accepted_gcp_image_digests="sha256:" + "22" * 32,
    )

    response = gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(boot_auth_header=header),
        GatewayAuthorizeRequest(**body_dict),
        settings,
        raw_body,
    )
    _wait_for_shadow_delivery()

    assert "spend_lease" not in response["data"]
    event = STORE.spend_lease_shadow_events[response["data"]["authorization_id"]]
    assert event["boot_verified"] is False
    assert event["no_lease_reason"] == "boot_digest_not_accepted"


def test_authorize_shadow_events_include_accept_and_decline_and_keep_echo_invalid() -> None:
    workspace, key, _private, _boot = _seed_authorize()
    settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_pilot_workspace_ids=workspace.id,
        spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
    )
    accepted_raw = {
        "api_key_lookup_hash": key.lookup_hash,
        "model": "anthropic/claude-haiku-4.5",
        "route_type": "chat.completions",
    }
    gateway._authorize_gateway_sync(  # noqa: SLF001
        _request(),
        GatewayAuthorizeRequest(**accepted_raw),
        settings,
        json.dumps(accepted_raw).encode(),
    )
    declined_raw = {
        "api_key_lookup_hash": "missing-key",
        "model": "anthropic/claude-haiku-4.5",
        "route_type": "chat.completions",
    }
    with pytest.raises(HTTPException):
        gateway._authorize_gateway_sync(  # noqa: SLF001
            _request(),
            GatewayAuthorizeRequest(**declined_raw),
            settings,
            json.dumps(declined_raw).encode(),
        )
    _wait_for_shadow_delivery()
    events = list(STORE.spend_lease_shadow_events.values())
    assert {event["server_verdict"] for event in events} == {"accepted", "declined_other"}
    assert all(event["divergence"] == "echo_invalid" for event in events)


def test_authorize_gateway_forwards_exact_cached_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_body = b'{ "api_key_lookup_hash" : "lookup", "model" : "model" }'
    body = GatewayAuthorizeRequest(**json.loads(raw_body))
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/internal/gateway/authorize",
            "headers": [],
        },
        receive,
    )
    captured: dict[str, bytes] = {}

    def authorize_sync(
        _request: Request,
        _body: GatewayAuthorizeRequest,
        _settings: Settings,
        exact_body_bytes: bytes,
    ) -> dict[str, Any]:
        captured["body"] = exact_body_bytes
        return {"data": {"authorization_id": "gwa-exact-body"}}

    monkeypatch.setattr(gateway, "_authorize_gateway_sync", authorize_sync)
    result = asyncio.run(
        gateway.authorize_gateway(request, body, Settings(environment="test"))
    )
    assert result["data"]["authorization_id"] == "gwa-exact-body"
    assert captured["body"] == raw_body


def test_authorize_body_schema_has_no_boot_auth_member() -> None:
    assert "boot_auth" not in GatewayAuthorizeRequest.model_fields
