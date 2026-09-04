from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.receipt_keys import b64url_decode, b64url_encode
from trusted_router.schemas import (
    SpendLeaseAdmissionMarker,
    SpendLeaseAdmissionRejected,
)
from trusted_router.spend_lease_admission import ADMISSION_REFUSAL_REASONS
from trusted_router.spend_leases import boot_auth_digest, parse_boot_auth_header

FIXTURES = Path(__file__).parent / "fixtures" / "stage_c"


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _json(name: str) -> dict:
    return json.loads(_bytes(name))


def _assert_canonical_json(name: str) -> dict:
    raw = _bytes(name)
    value = json.loads(raw)
    assert raw == json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return value


def test_fixture_manifest_is_the_exact_stage_c_wire_set() -> None:
    fixed = {
        "admission_accepted_response.json",
        "admission_receipt_compact.jws",
        "admission_receipt_ed25519_seed.hex",
        "admission_receipt_payload.json",
        "admission_receipt_protected_header.json",
        "admission_receipt_verification_jwk.json",
        "authoritative_lease_compact.jws",
        "authoritative_lease_payload.json",
        "authoritative_lease_protected_header.json",
        "normalized_routing_inputs.json",
        "normalized_routing_inputs.sha256",
        "receipt_bearing_authorize_boot_auth.txt",
        "receipt_bearing_authorize_request.json",
    }
    rejections = {
        f"admission_rejected_{reason}.json" for reason in ADMISSION_REFUSAL_REASONS
    }

    assert {path.name for path in FIXTURES.iterdir() if path.is_file()} == (
        fixed | rejections
    )


def test_authoritative_lease_fixture_segments_and_signature_are_exact() -> None:
    compact = _bytes("authoritative_lease_compact.jws").decode()
    protected, payload, signature = compact.split(".")
    seed = bytes.fromhex(_bytes("admission_receipt_ed25519_seed.hex").decode())
    private = Ed25519PrivateKey.from_private_bytes(seed)

    assert b64url_decode(protected) == _bytes(
        "authoritative_lease_protected_header.json"
    )
    assert b64url_decode(payload) == _bytes("authoritative_lease_payload.json")
    private.public_key().verify(
        b64url_decode(signature), f"{protected}.{payload}".encode()
    )
    claims = _assert_canonical_json("authoritative_lease_payload.json")
    assert claims["authoritative"] is True
    assert claims["local_admission_allowed"] is True
    assert claims["routing_policy_hash"] == _bytes(
        "normalized_routing_inputs.sha256"
    ).decode()
    assert set(claims["catalog"]["candidates"][0]) >= {
        "upstream_model",
        "usage_type",
        "wafer_zdr_required",
    }


def test_normalized_routing_fixture_hash_is_exact() -> None:
    normalized = _bytes("normalized_routing_inputs.json")

    _assert_canonical_json("normalized_routing_inputs.json")
    assert hashlib.sha256(normalized).hexdigest().encode() == _bytes(
        "normalized_routing_inputs.sha256"
    )


def test_admission_receipt_fixture_segments_signature_and_jwk_are_exact() -> None:
    compact = _bytes("admission_receipt_compact.jws").decode()
    protected, payload, signature = compact.split(".")
    seed = bytes.fromhex(_bytes("admission_receipt_ed25519_seed.hex").decode())
    private = Ed25519PrivateKey.from_private_bytes(seed)
    jwk = _assert_canonical_json("admission_receipt_verification_jwk.json")

    assert jwk == {
        "crv": "Ed25519",
        "kty": "OKP",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }
    assert b64url_decode(protected) == _bytes(
        "admission_receipt_protected_header.json"
    )
    assert b64url_decode(payload) == _bytes("admission_receipt_payload.json")
    private.public_key().verify(
        b64url_decode(signature), f"{protected}.{payload}".encode()
    )


def test_receipt_bearing_request_and_boot_auth_are_exact_bytes() -> None:
    raw = _bytes("receipt_bearing_authorize_request.json")
    request = _assert_canonical_json("receipt_bearing_authorize_request.json")
    auth = parse_boot_auth_header(
        _bytes("receipt_bearing_authorize_boot_auth.txt").decode()
    )
    assert auth is not None
    assert request["spend_lease_admission"].encode() == _bytes(
        "admission_receipt_compact.jws"
    )
    seed = bytes.fromhex(_bytes("admission_receipt_ed25519_seed.hex").decode())
    Ed25519PrivateKey.from_private_bytes(seed).public_key().verify(
        b64url_decode(auth.signature),
        boot_auth_digest("POST", "/v1/internal/gateway/authorize", raw),
    )


def test_accepted_response_binds_authorization_snapshot_remaining_and_marker() -> None:
    response = _assert_canonical_json("admission_accepted_response.json")["data"]
    lease = _json("authoritative_lease_payload.json")
    receipt = _bytes("admission_receipt_compact.jws")
    receipt_payload = _json("admission_receipt_payload.json")

    assert response["authorization_id"] == "gwa-stage-c-fixture"
    assert response["estimated_cost_microdollars"] == receipt_payload[
        "enclave_estimate_micro"
    ]
    assert response["spend_lease"]["remaining_micro"] == receipt_payload[
        "remaining_after_micro"
    ]
    assert response["spend_lease"]["token"].encode() == _bytes(
        "authoritative_lease_compact.jws"
    )
    assert response["spend_lease_admission"] == {
        "accepted": True,
        "receipt_hash": hashlib.sha256(receipt).hexdigest(),
    }
    SpendLeaseAdmissionMarker.model_validate(response["spend_lease_admission"])
    for field in ("upstream_model", "usage_type", "wafer_zdr_required"):
        expected = lease["catalog"]["candidates"][0][field]
        assert response["route_candidates"][0].get(field, False) == expected


def test_every_closed_rejection_response_is_canonical_and_named() -> None:
    observed = set()
    for reason in ADMISSION_REFUSAL_REASONS:
        body = _assert_canonical_json(f"admission_rejected_{reason}.json")
        assert body == {
            "error": {
                "code": 409,
                "message": "Spend-lease admission was rejected",
                "reason": reason,
                "source": "router",
                "type": "admission_rejected",
            }
        }
        SpendLeaseAdmissionRejected.model_validate(body)
        observed.add(body["error"]["reason"])
    assert observed == ADMISSION_REFUSAL_REASONS
