from __future__ import annotations

import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.gateway_boot import (
    BootAuthHeader,
    SpendLeaseBoot,
    boot_auth_digest,
    parse_boot_auth_header,
    verify_boot_auth,
)
from trusted_router.receipt_keys import b64url_encode


def _boot_auth_fixture() -> tuple[Ed25519PrivateKey, SpendLeaseBoot, bytes, BootAuthHeader]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public)}
    boot = SpendLeaseBoot(
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
    unverified = SpendLeaseBoot(**{**boot.__dict__, "verified": False})
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
