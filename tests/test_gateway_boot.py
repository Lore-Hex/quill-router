from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.gateway_boot import (
    BootAuthHeader,
    GatewayBoot,
    boot_auth_digest,
    parse_boot_auth_header,
    verify_boot_auth,
)
from trusted_router.receipt_keys import b64url_encode, receipt_kid
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest, GatewayBootRegistrationRequest
from trusted_router.storage import STORE


def _boot_auth_fixture() -> tuple[Ed25519PrivateKey, GatewayBoot, bytes, BootAuthHeader]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public)}
    boot = GatewayBoot(
        kid="boot-kid",
        jwk=jwk,
        approved=True,
        verified=True,
        image_digest="sha256:" + "11" * 32,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-08-27T00:00:00Z",
    )
    body: dict[str, object] = {
        "api_key_lookup_hash": "lookup",
        "model": "vendor/model",
        "estimated_input_tokens": 10,
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = private.sign(boot_auth_digest("POST", "/v1/internal/gateway/authorize", raw_body))
    auth = BootAuthHeader(kid=boot.kid, signature=b64url_encode(signature))
    return private, boot, raw_body, auth


def test_boot_auth_verifies_exact_body_bytes_and_resolved_lookup_hash() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    assert verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={boot.image_digest},
    )
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="different",
        accepted_image_digests={boot.image_digest},
    )


def test_boot_auth_digest_matches_v1_wire_formula_and_uppercases_method() -> None:
    raw_body = b'{ "model": "vendor/model" }'
    path = "/v1/internal/gateway/authorize"
    expected = hashlib.sha256(
        b"tr-authorize-v1" + b"POST" + path.encode() + hashlib.sha256(raw_body).digest()
    ).digest()
    assert boot_auth_digest("post", path, raw_body) == expected


def test_boot_auth_rejects_one_byte_tamper_inside_received_body() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    mutated = bytearray(raw_body)
    target = b'"estimated_input_tokens":10'
    changed_byte = raw_body.index(target) + len(target) - 1
    mutated[changed_byte] = ord("1")
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=bytes(mutated),
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={boot.image_digest},
    )


def test_boot_auth_refuses_unknown_digest_even_if_persisted_approved() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    assert boot.approved is True
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={"sha256:" + "22" * 32},
    )


def test_boot_auth_empty_current_accepted_set_refuses_every_digest() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests=frozenset(),
    )


def test_boot_auth_unverified_or_mismatched_boot_is_refused() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    unverified = GatewayBoot(**{**boot.__dict__, "verified": False})
    assert not verify_boot_auth(
        boot=unverified,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={boot.image_digest},
    )
    assert not verify_boot_auth(
        boot=None,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={boot.image_digest},
    )
    assert not verify_boot_auth(
        boot=boot,
        auth=BootAuthHeader(kid="other-kid", signature=auth.signature),
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={boot.image_digest},
    )


def test_boot_auth_header_parser_rejects_ambiguous_values() -> None:
    assert parse_boot_auth_header("kid=boot,sig=abc") == BootAuthHeader("boot", "abc")
    assert parse_boot_auth_header("kid=one,kid=two,sig=abc") is None
    assert parse_boot_auth_header("kid=boot,sig=abc,extra=value") is None
    assert parse_boot_auth_header("kid=boot") is None
    assert parse_boot_auth_header(None) is None


# ---- boot registration: the attested enclave's one-time identity handshake --

REGISTER_PATH = "/v1/internal/gateway/spend-lease/register-boot"


def _request(path: str = REGISTER_PATH) -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


def _jwk(private: Ed25519PrivateKey) -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }


def _registration_settings(digest: str) -> Settings:
    return Settings(environment="test", spend_lease_accepted_gcp_image_digests=digest)


def test_boot_registration_accepts_verified_gcp_approved_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    digest = "sha256:" + "11" * 32
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    monkeypatch.setattr(gateway, "verify_gcp_attestation_chain", lambda _att: None)
    monkeypatch.setattr(gateway, "gcp_attestation_image_digest", lambda _att: digest)
    response = gateway._register_gateway_boot_sync(  # noqa: SLF001
        _request(),
        GatewayBootRegistrationRequest(
            kid=receipt_kid(jwk),
            receipt_public_key=jwk,
            attestation_evidence="signed-gcp-evidence",
            attestation_kind="gcp",
        ),
        _registration_settings(digest),
    )
    assert response == {"data": {"verified": True}}
    stored = STORE.get_gateway_boot(receipt_kid(jwk))
    assert stored is not None and stored.verified is True and stored.approved is True
    assert stored.image_digest == digest


def test_boot_registration_kid_must_match_the_receipt_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    with pytest.raises(HTTPException, match="kid does not match"):
        gateway._register_gateway_boot_sync(  # noqa: SLF001
            _request(),
            GatewayBootRegistrationRequest(
                kid="someone-else",
                receipt_public_key=jwk,
                attestation_evidence="signed-gcp-evidence",
                attestation_kind="gcp",
            ),
            _registration_settings("sha256:" + "11" * 32),
        )
    assert STORE.get_gateway_boot(receipt_kid(jwk)) is None


def test_boot_registration_rejects_evidence_that_does_not_commit_to_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: False)
    with pytest.raises(HTTPException, match="does not commit"):
        gateway._register_gateway_boot_sync(  # noqa: SLF001
            _request(),
            GatewayBootRegistrationRequest(
                kid=receipt_kid(jwk),
                receipt_public_key=jwk,
                attestation_evidence="unbound",
                attestation_kind="gcp",
            ),
            _registration_settings("sha256:" + "11" * 32),
        )
    assert STORE.get_gateway_boot(receipt_kid(jwk)) is None


def test_boot_registration_records_wrong_gcp_image_digest_as_unapproved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    configured = "sha256:" + "11" * 32
    observed = "sha256:" + "22" * 32
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    monkeypatch.setattr(gateway, "verify_gcp_attestation_chain", lambda _att: None)
    monkeypatch.setattr(gateway, "gcp_attestation_image_digest", lambda _att: observed)
    response = gateway._register_gateway_boot_sync(  # noqa: SLF001
        _request(),
        GatewayBootRegistrationRequest(
            kid=receipt_kid(jwk),
            receipt_public_key=jwk,
            attestation_evidence="signed-gcp-evidence",
            attestation_kind="gcp",
        ),
        _registration_settings(configured),
    )
    # Verified (the chain is good) but not approved: acceptance is decided at
    # authorize time against the live digest set, never at registration.
    assert response == {"data": {"verified": True}}
    stored = STORE.get_gateway_boot(receipt_kid(jwk))
    assert stored is not None and stored.approved is False and stored.image_digest == observed


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
        gateway._register_gateway_boot_sync(  # noqa: SLF001
            _request(),
            GatewayBootRegistrationRequest(
                kid=receipt_kid(jwk),
                receipt_public_key=jwk,
                attestation_evidence="forged",
                attestation_kind="gcp",
            ),
            _registration_settings("sha256:" + "11" * 32),
        )
    assert STORE.get_gateway_boot(receipt_kid(jwk)) is None


def test_boot_registration_records_aws_as_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    STORE.reset()
    jwk = _jwk(Ed25519PrivateKey.generate())
    monkeypatch.setattr(gateway, "attestation_commits_to_jwk", lambda *_args: True)
    response = gateway._register_gateway_boot_sync(  # noqa: SLF001
        _request(),
        GatewayBootRegistrationRequest(
            kid=receipt_kid(jwk),
            receipt_public_key=jwk,
            attestation_evidence="bound-aws-cose",
            attestation_kind="aws",
        ),
        _registration_settings("sha256:" + "11" * 32),
    )
    assert response == {"data": {"verified": False}}
    stored = STORE.get_gateway_boot(receipt_kid(jwk))
    assert stored is not None and stored.verified is False and stored.approved is False


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


def test_authorize_gateway_forwards_exact_cached_body_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The boot signature covers the request bytes exactly as the enclave sent
    # them. The async endpoint must hand those cached bytes, not a
    # re-serialization of the parsed model, to the verifier.
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
    result = asyncio.run(gateway.authorize_gateway(request, body, Settings(environment="test")))
    assert result["data"]["authorization_id"] == "gwa-exact-body"
    assert captured["body"] == raw_body
