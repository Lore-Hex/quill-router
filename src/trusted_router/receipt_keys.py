"""Validation and append-only merge rules for inference-receipt keys."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

import cbor2
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from trusted_router.storage_models import ReceiptKey

logger = logging.getLogger(__name__)

RECEIPT_KEY_KIND = "receipt_key"
RECEIPT_KEY_COMMITMENT_DOMAIN = b"inference-receipt-key-v1"
GCP_ATTESTATION_KIND = "gcp-cs-jwt"
AWS_ATTESTATION_KIND = "aws-nitro-cose"
AZURE_ATTESTATION_KIND = "azure-maa-jwt"
SUPPORTED_ATTESTATION_KINDS = frozenset(
    {GCP_ATTESTATION_KIND, AWS_ATTESTATION_KIND, AZURE_ATTESTATION_KIND}
)
GCP_ISSUER = "https://confidentialcomputing.googleapis.com"
GCP_AUDIENCE = "quill-cloud"
GCP_CLOCK_SKEW_SECONDS = 300
_GCP_OPENID_CONFIGURATION_URL = f"{GCP_ISSUER}/.well-known/openid-configuration"
_GCP_JWKS_CACHE_SECONDS = 3600
_GCP_JWKS_CACHE: tuple[float, dict[str, Any]] | None = None
_GCP_JWKS_LOCK = threading.Lock()

ReceiptKeyWriteOutcome = Literal["appended", "refreshed", "conflict", "invalid"]


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    if not value or any(char.isspace() for char in value):
        raise ValueError("empty or whitespace-bearing base64url value")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url value") from exc


def normalize_receipt_jwk(jwk: Mapping[str, Any]) -> dict[str, str]:
    normalized = {
        "kty": jwk.get("kty"),
        "crv": jwk.get("crv"),
        "x": jwk.get("x"),
    }
    if normalized["kty"] != "OKP" or normalized["crv"] != "Ed25519":
        raise ValueError("receipt JWK must be an OKP Ed25519 public key")
    if not isinstance(normalized["x"], str):
        raise ValueError("receipt JWK x must be a string")
    public_key = b64url_decode(normalized["x"])
    if len(public_key) != 32:
        raise ValueError("receipt JWK x must decode to 32 bytes")
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": normalized["x"],
    }


def receipt_kid(jwk: Mapping[str, Any]) -> str:
    public_key = b64url_decode(normalize_receipt_jwk(jwk)["x"])
    return b64url_encode(hashlib.sha256(public_key).digest())


def receipt_key_commitment(jwk: Mapping[str, Any]) -> bytes:
    public_key = b64url_decode(normalize_receipt_jwk(jwk)["x"])
    return hashlib.sha256(RECEIPT_KEY_COMMITMENT_DOMAIN + b"\x00" + public_key).digest()


def _parse_jwt(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header = json.loads(b64url_decode(header_segment))
        payload = json.loads(b64url_decode(payload_segment))
        signature = b64url_decode(signature_segment)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("attestation is not a valid compact JWT") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("attestation JWT header and payload must be objects")
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    return header, payload, signing_input, signature


def _walk_named_values(node: Any, name: str, *, depth: int = 0) -> Iterator[Any]:
    if depth > 12:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).casefold() == name.casefold():
                yield value
            yield from _walk_named_values(value, name, depth=depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk_named_values(value, name, depth=depth + 1)


def _flatten_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten_strings(item)


def _gcp_committed_values(payload: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for claim_name in ("eat_nonce", "nonces"):
        for claim in _walk_named_values(payload, claim_name):
            values.update(value.strip().casefold() for value in _flatten_strings(claim))
    return values


def _decode_aws_payload(attestation: str) -> dict[Any, Any]:
    try:
        decoded: Any = cbor2.loads(b64url_decode(attestation))
        if isinstance(decoded, cbor2.CBORTag):
            decoded = decoded.value
        if not isinstance(decoded, (list, tuple)) or len(decoded) != 4:
            raise ValueError("AWS attestation is not COSE_Sign1")
        payload_bytes = decoded[2]
        if not isinstance(payload_bytes, bytes):
            raise ValueError("AWS COSE payload is not bytes")
        payload = cbor2.loads(payload_bytes)
    except (cbor2.CBORDecodeError, ValueError) as exc:
        raise ValueError("AWS attestation is not valid COSE_Sign1") from exc
    if not isinstance(payload, dict):
        raise ValueError("AWS attestation payload is not an object")
    return payload


def _decode_json_runtime_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    candidates: list[bytes] = []
    try:
        candidates.append(base64.b64decode(value, validate=True))
    except (ValueError, TypeError):
        pass
    try:
        candidates.append(b64url_decode(value))
    except ValueError:
        pass
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _azure_runtime(payload: dict[str, Any]) -> Any:
    for claim_name in ("x-ms-runtime", "runtime_data", "runtimeData"):
        for value in _walk_named_values(payload, claim_name):
            decoded = _decode_json_runtime_value(value)
            if decoded is not None:
                return decoded
    return None


def attestation_commits_to_jwk(att: str, att_kind: str, jwk: Mapping[str, Any]) -> bool:
    """Return whether the attestation's provider-defined key slot contains C.

    This checks the signed-document commitment shape, not its trust chain.
    Chain verification is deliberately a separate verdict so AWS/Azure keys
    may be published as ``verified=false`` without ever accepting an unbound
    key.
    """

    commitment = receipt_key_commitment(jwk)
    if att_kind == GCP_ATTESTATION_KIND:
        _, payload, _, _ = _parse_jwt(att)
        return commitment.hex() in _gcp_committed_values(payload)
    if att_kind == AWS_ATTESTATION_KIND:
        user_data = _decode_aws_payload(att).get("user_data")
        return (
            isinstance(user_data, bytes)
            and len(user_data) == 128
            and user_data[96:128] == commitment
        )
    if att_kind == AZURE_ATTESTATION_KIND:
        _, payload, _, _ = _parse_jwt(att)
        runtime = _azure_runtime(payload)
        values = {
            value.strip().casefold()
            for item in _walk_named_values(runtime, "receipt_key_fp")
            for value in _flatten_strings(item)
        }
        return commitment.hex() in values
    raise ValueError(f"unsupported receipt attestation kind: {att_kind!r}")


def validate_receipt_key_observation(record: ReceiptKey) -> ReceiptKey:
    """Validate and narrow an observation before it reaches durable storage."""

    jwk = normalize_receipt_jwk(record.jwk)
    if record.kid != receipt_kid(jwk):
        raise ValueError("receipt kid does not match SHA-256 of JWK x")
    if record.att_kind not in SUPPORTED_ATTESTATION_KINDS:
        raise ValueError("unsupported receipt attestation kind")
    if not isinstance(record.att, str) or not record.att:
        raise ValueError("receipt attestation must be a non-empty string")
    if not record.plane:
        raise ValueError("receipt key plane must be non-empty")
    if not attestation_commits_to_jwk(record.att, record.att_kind, jwk):
        raise ValueError("attestation does not commit to the receipt JWK")
    return dataclasses.replace(record, jwk=jwk)


def merge_receipt_key_observation(
    existing: ReceiptKey | None,
    observed: ReceiptKey,
) -> tuple[ReceiptKey | None, ReceiptKeyWriteOutcome]:
    """Apply the only state transition allowed for one receipt ``kid``."""

    if existing is not None:
        try:
            existing_jwk = normalize_receipt_jwk(existing.jwk)
            observed_jwk = normalize_receipt_jwk(observed.jwk)
        except ValueError:
            # The full validator below emits the more useful malformed-record
            # alarm. Persisted rows have already passed this check.
            pass
        else:
            if existing_jwk != observed_jwk:
                logger.error(
                    "ALERT receipt_key_kid_collision kid=%s existing_plane=%s observed_plane=%s",
                    observed.kid,
                    existing.plane,
                    observed.plane,
                )
                return existing, "conflict"

    try:
        observed = validate_receipt_key_observation(observed)
    except ValueError as exc:
        logger.error(
            "receipt_key_invalid_observation kid=%s reason=%s",
            observed.kid,
            exc,
        )
        return existing, "invalid"
    if existing is None:
        return observed, "appended"

    existing_jwk = normalize_receipt_jwk(existing.jwk)
    if existing.att_kind != observed.att_kind:
        logger.error(
            "ALERT receipt_key_attestation_kind_changed kid=%s existing=%s observed=%s",
            observed.kid,
            existing.att_kind,
            observed.att_kind,
        )
        return existing, "conflict"

    # The incoming attestation was checked against this exact JWK above, so it
    # is safe to refresh.  Never change first_seen, plane, or revocation state;
    # and never let a transient verifier failure downgrade a prior success.
    advances_clock = observed.last_seen >= existing.last_seen
    return (
        dataclasses.replace(
            existing,
            jwk=existing_jwk,
            att=observed.att if advances_clock else existing.att,
            last_seen=max(existing.last_seen, observed.last_seen),
            verified=existing.verified or observed.verified,
        ),
        "refreshed",
    )


def _rsa_key_from_jwk(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    if jwk.get("kty") != "RSA":
        raise ValueError("GCP JWKS key is not RSA")
    try:
        modulus = int.from_bytes(b64url_decode(str(jwk["n"])), "big")
        exponent = int.from_bytes(b64url_decode(str(jwk["e"])), "big")
        return rsa.RSAPublicNumbers(e=exponent, n=modulus).public_key()
    except (KeyError, ValueError) as exc:
        raise ValueError("GCP JWKS RSA key is malformed") from exc


def _fetch_gcp_jwks() -> dict[str, Any]:
    global _GCP_JWKS_CACHE
    now = time.monotonic()
    with _GCP_JWKS_LOCK:
        if _GCP_JWKS_CACHE is not None and now - _GCP_JWKS_CACHE[0] < _GCP_JWKS_CACHE_SECONDS:
            return _GCP_JWKS_CACHE[1]
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            config_response = client.get(_GCP_OPENID_CONFIGURATION_URL)
            config_response.raise_for_status()
            config = config_response.json()
            jwks_uri = config.get("jwks_uri") if isinstance(config, dict) else None
            parsed_jwks_uri = urlsplit(jwks_uri) if isinstance(jwks_uri, str) else None
            if (
                not isinstance(jwks_uri, str)
                or parsed_jwks_uri is None
                or parsed_jwks_uri.scheme != "https"
                or not parsed_jwks_uri.hostname
                or parsed_jwks_uri.username is not None
                or parsed_jwks_uri.password is not None
            ):
                raise ValueError("GCP OpenID metadata returned an untrusted JWKS URI")
            jwks_response = client.get(jwks_uri)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise ValueError("GCP JWKS response has no keys")
        _GCP_JWKS_CACHE = (now, jwks)
        return jwks


def verify_gcp_attestation_chain(
    att: str,
    *,
    now: float | None = None,
    jwks: Mapping[str, Any] | None = None,
) -> None:
    """Verify the minimal GCP Confidential Space JWT trust chain."""

    header, payload, signing_input, signature = _parse_jwt(att)
    if header.get("alg") != "RS256":
        raise ValueError("GCP attestation JWT algorithm is not RS256")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise ValueError("GCP attestation JWT has no kid")
    if payload.get("iss") != GCP_ISSUER:
        raise ValueError("GCP attestation JWT issuer is not Confidential Space")
    audience = payload.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if GCP_AUDIENCE not in audiences:
        raise ValueError("GCP attestation JWT audience is invalid")

    key_set = jwks if jwks is not None else _fetch_gcp_jwks()
    keys = key_set.get("keys")
    if not isinstance(keys, list):
        raise ValueError("GCP JWKS response has no keys")
    signing_jwk = next(
        (item for item in keys if isinstance(item, dict) and item.get("kid") == kid),
        None,
    )
    if signing_jwk is None:
        raise ValueError("GCP attestation JWT kid is absent from issuer JWKS")
    try:
        _rsa_key_from_jwk(signing_jwk).verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise ValueError("GCP attestation JWT signature is invalid") from exc

    current = time.time() if now is None else now
    expires_at = payload.get("exp")
    not_before = payload.get("nbf")
    issued_at = payload.get("iat")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("GCP attestation JWT has no numeric exp")
    if current > float(expires_at) + GCP_CLOCK_SKEW_SECONDS:
        raise ValueError("GCP attestation JWT is expired")
    for claim_name, claim_value in (("nbf", not_before), ("iat", issued_at)):
        if claim_value is not None and (
            isinstance(claim_value, bool) or not isinstance(claim_value, (int, float))
        ):
            raise ValueError(f"GCP attestation JWT has a non-numeric {claim_name}")
    if not_before is not None and current + GCP_CLOCK_SKEW_SECONDS < float(not_before):
        raise ValueError("GCP attestation JWT is not valid yet")
    if issued_at is not None and current + GCP_CLOCK_SKEW_SECONDS < float(issued_at):
        raise ValueError("GCP attestation JWT was issued in the future")

    debug_values = list(_walk_named_values(payload, "dbgstat"))
    bad_debug_values = {
        str(value).strip().casefold()
        for value in debug_values
        if str(value).strip().casefold() in {"enabled", "enable", "true", "1", "debug"}
    }
    if bad_debug_values:
        raise ValueError("GCP Confidential Space debug status is enabled")


def gcp_attestation_image_digest(att: str) -> str:
    """Extract the single measured image digest from a parsed GCP JWT.

    Chain verification is deliberately separate so tests can inject issuer
    keys and callers cannot mistake extraction for authentication.
    """
    _, payload, _, _ = _parse_jwt(att)
    values = {
        value.strip().casefold()
        for item in _walk_named_values(payload, "image_digest")
        for value in _flatten_strings(item)
        if value.strip()
    }
    if len(values) != 1:
        raise ValueError("GCP attestation must carry exactly one image digest")
    digest = next(iter(values))
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("GCP attestation image digest is malformed")
    try:
        bytes.fromhex(digest.removeprefix("sha256:"))
    except ValueError as exc:
        raise ValueError("GCP attestation image digest is malformed") from exc
    return digest
