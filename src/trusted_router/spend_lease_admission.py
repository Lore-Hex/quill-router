"""Stage C admission-receipt wire primitives.

The router remains the authority: an enclave receipt proves that one attested
boot admitted a request against one lease, but it never creates an
authorization by itself.  This module is deliberately pure so the exact wire
contract can be shared by route, storage, and fixture tests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from trusted_router.receipt_keys import b64url_decode, normalize_receipt_jwk
from trusted_router.spend_leases import SpendLeaseBoot

SPEND_LEASE_ADMISSION_TYP = "spend_lease_admission+jws"
SPEND_LEASE_ADMISSION_VERSION = 1

ADMISSION_RECEIPT_PAYLOAD_FIELDS = frozenset(
    {
        "v",
        "lease_id",
        "gen",
        "key_hash",
        "workspace_id",
        "boot_kid",
        "idempotency_key_sha256",
        "routing_policy_hash",
        "enclave_estimate_micro",
        "remaining_after_micro",
        "admitted_at_ms",
    }
)


class AdmissionRefusalReason(StrEnum):
    RECEIPT_INVALID = "receipt_invalid"
    BOOT_NOT_ACCEPTED = "boot_not_accepted"
    BOOT_MISMATCH = "boot_mismatch"
    LEASE_NOT_OPEN = "lease_not_open"
    WINDOW = "window"
    POLICY_MISMATCH = "policy_mismatch"
    ESTIMATE_MISMATCH = "estimate_mismatch"
    CAPACITY = "capacity"
    HOLD_REFUSED = "hold_refused"
    SCOPE_CONFLICT = "scope_conflict"
    REUSE_LOST = "reuse_lost"
    NOT_ACCEPTING = "not_accepting"


ADMISSION_REFUSAL_REASONS = frozenset(reason.value for reason in AdmissionRefusalReason)


class AdmissionReceiptError(ValueError):
    """A compact receipt is malformed or has an invalid signature."""


@dataclass(frozen=True, slots=True)
class AdmissionReceiptClaims:
    v: int
    lease_id: str
    gen: int
    key_hash: str
    workspace_id: str
    boot_kid: str
    idempotency_key_sha256: str
    routing_policy_hash: str
    enclave_estimate_micro: int
    remaining_after_micro: int
    admitted_at_ms: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AdmissionReceiptClaims:
        if set(value) != ADMISSION_RECEIPT_PAYLOAD_FIELDS:
            raise AdmissionReceiptError("receipt payload fields do not match v1")
        integer_fields = {
            "v",
            "gen",
            "enclave_estimate_micro",
            "remaining_after_micro",
            "admitted_at_ms",
        }
        if any(type(value[field]) is not int for field in integer_fields):
            raise AdmissionReceiptError("receipt integer fields must be JSON integers")
        string_fields = ADMISSION_RECEIPT_PAYLOAD_FIELDS - integer_fields
        if any(not isinstance(value[field], str) or not value[field] for field in string_fields):
            raise AdmissionReceiptError("receipt string fields must be non-empty")
        if value["v"] != SPEND_LEASE_ADMISSION_VERSION or value["gen"] <= 0:
            raise AdmissionReceiptError("unsupported receipt version or generation")
        if value["enclave_estimate_micro"] <= 0 or value["remaining_after_micro"] < 0:
            raise AdmissionReceiptError("receipt monetary fields are out of range")
        for field in ("idempotency_key_sha256", "routing_policy_hash"):
            candidate = value[field]
            if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
                raise AdmissionReceiptError(f"{field} must be lowercase SHA-256 hex")
        return cls(**{field: value[field] for field in ADMISSION_RECEIPT_PAYLOAD_FIELDS})


@dataclass(frozen=True, slots=True)
class ParsedAdmissionReceipt:
    protected: dict[str, str]
    claims: AdmissionReceiptClaims
    signing_input: bytes
    signature: bytes


def parse_admission_receipt(compact: str) -> ParsedAdmissionReceipt:
    """Parse the strict compact JWS without trusting any of its claims."""

    if not compact or compact.count(".") != 2:
        raise AdmissionReceiptError("receipt must be a compact JWS")
    protected_segment, payload_segment, signature_segment = compact.split(".")
    try:
        protected_value = json.loads(b64url_decode(protected_segment))
        payload_value = json.loads(b64url_decode(payload_segment))
        signature = b64url_decode(signature_segment)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdmissionReceiptError("receipt is not valid base64url JSON") from exc
    if not isinstance(protected_value, dict) or not isinstance(payload_value, dict):
        raise AdmissionReceiptError("receipt header and payload must be objects")
    if set(protected_value) != {"alg", "typ", "kid"} or protected_value.get("alg") != "EdDSA" or protected_value.get("typ") != SPEND_LEASE_ADMISSION_TYP:
        raise AdmissionReceiptError("receipt protected header does not match Stage C")
    kid = protected_value.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AdmissionReceiptError("receipt protected kid is required")
    if len(signature) != 64:
        raise AdmissionReceiptError("receipt Ed25519 signature must be 64 bytes")
    claims = AdmissionReceiptClaims.from_mapping(payload_value)
    protected = {str(key): str(value) for key, value in protected_value.items()}
    return ParsedAdmissionReceipt(
        protected=protected,
        claims=claims,
        signing_input=f"{protected_segment}.{payload_segment}".encode("ascii"),
        signature=signature,
    )


def verify_admission_receipt(compact: str, boot: SpendLeaseBoot | None) -> ParsedAdmissionReceipt:
    """Verify one receipt under the JWK registered for its protected kid."""

    parsed = parse_admission_receipt(compact)
    if parsed.protected["kid"] != parsed.claims.boot_kid:
        raise AdmissionReceiptError("receipt boot ownership mismatch")
    if boot is None or boot.kid != parsed.claims.boot_kid:
        raise AdmissionReceiptError("receipt boot is unavailable")
    try:
        public_bytes = b64url_decode(normalize_receipt_jwk(boot.jwk)["x"])
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            parsed.signature,
            parsed.signing_input,
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise AdmissionReceiptError("receipt signature is invalid") from exc
    return parsed


def parse_authoritative_lease_token(compact: str) -> dict[str, Any]:
    """Decode the immutable router-minted token stored on the lease row."""

    if not compact or compact.count(".") != 2:
        raise AdmissionReceiptError("lease token must be a compact JWS")
    protected_segment, payload_segment, _signature_segment = compact.split(".")
    try:
        protected = json.loads(b64url_decode(protected_segment))
        payload = json.loads(b64url_decode(payload_segment))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdmissionReceiptError("lease token is not valid base64url JSON") from exc
    if (
        not isinstance(protected, dict)
        or set(protected) != {"alg", "kid", "typ"}
        or protected.get("alg") != "EdDSA"
        or protected.get("typ") != "spend-lease+jws"
        or not isinstance(payload, dict)
        or payload.get("authoritative") is not True
        or payload.get("local_admission_allowed") is not True
    ):
        raise AdmissionReceiptError("lease token is not authoritative for Stage C")
    return payload


def receipt_hash(compact: str) -> str:
    return hashlib.sha256(compact.encode("ascii")).hexdigest()


def idempotency_key_hash(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def classify_receipt_replay(
    incoming_hash: str | None,
    stored_hash: str | None,
) -> str:
    """Decision 60's complete replay table, shared by both replay sites."""

    if incoming_hash is None and stored_hash is None:
        return "ordinary"
    if incoming_hash is not None and incoming_hash == stored_hash:
        return "replay"
    return "scope_conflict"
