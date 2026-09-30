"""Attested gateway boot identity and per-request boot authentication.

An attested enclave registers its receipt public key once per boot at
``POST /internal/gateway/spend-lease/register-boot`` (the path and the
``spend_lease_boot`` entity kind predate the spend-lease pilot's removal and
stay fixed for the deployed enclave fleet). Afterwards it signs the exact
bytes of every authorize, heartbeat and disposition request with that key in
the ``X-TR-Boot-Auth`` header. Stage D uses the verified boot to admit a
request into the heartbeat cohort; nothing here mutates credits.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from trusted_router.receipt_keys import b64url_decode, normalize_receipt_jwk
from trusted_router.storage_models import GatewayBoot as GatewayBoot

BOOT_AUTH_DOMAIN = b"tr-authorize-v1"
GATEWAY_BOOT_KIND = "spend_lease_boot"


@dataclass(frozen=True)
class BootAuthHeader:
    kid: str
    signature: str


def parse_boot_auth_header(value: str | None) -> BootAuthHeader | None:
    """Parse the v1 boot-auth header, rejecting malformed or ambiguous values."""
    if value is None:
        return None
    fields: dict[str, str] = {}
    for part in value.split(","):
        name, separator, field_value = part.strip().partition("=")
        if not separator or name in fields:
            return None
        fields[name] = field_value
    if set(fields) != {"kid", "sig"}:
        return None
    kid = fields["kid"]
    signature = fields["sig"]
    if not kid or len(kid) > 128 or not signature or len(signature) > 256:
        return None
    return BootAuthHeader(kid=kid, signature=signature)


def boot_auth_digest(method: str, path: str, exact_body_bytes: bytes) -> bytes:
    body_digest = hashlib.sha256(exact_body_bytes).digest()
    material = (
        BOOT_AUTH_DOMAIN + method.upper().encode("utf-8") + path.encode("utf-8") + body_digest
    )
    return hashlib.sha256(material).digest()


def verify_boot_auth(
    *,
    boot: GatewayBoot | None,
    auth: BootAuthHeader,
    method: str,
    path: str,
    exact_body_bytes: bytes,
    signed_lookup_hash: str | None,
    resolved_lookup_hash: str,
    accepted_image_digests: Collection[str],
) -> bool:
    """Verify a boot against the current trust config and its signed request."""
    return (
        _boot_auth_failure_reason(
            boot=boot,
            auth=auth,
            method=method,
            path=path,
            exact_body_bytes=exact_body_bytes,
            signed_lookup_hash=signed_lookup_hash,
            resolved_lookup_hash=resolved_lookup_hash,
            accepted_image_digests=accepted_image_digests,
        )
        is None
    )


def _boot_auth_failure_reason(
    *,
    boot: GatewayBoot | None,
    auth: BootAuthHeader,
    method: str,
    path: str,
    exact_body_bytes: bytes,
    signed_lookup_hash: str | None,
    resolved_lookup_hash: str,
    accepted_image_digests: Collection[str],
) -> Literal["boot_auth_invalid", "boot_digest_not_accepted"] | None:
    try:
        if boot is None or not boot.verified:
            return "boot_auth_invalid"
        if auth.kid != boot.kid:
            return "boot_auth_invalid"
        if signed_lookup_hash != resolved_lookup_hash:
            return "boot_auth_invalid"
        signature = b64url_decode(auth.signature)
        public_bytes = b64url_decode(normalize_receipt_jwk(boot.jwk)["x"])
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature,
            boot_auth_digest(method, path, exact_body_bytes),
        )
        if boot.image_digest not in accepted_image_digests:
            return "boot_digest_not_accepted"
        return None
    except (TypeError, ValueError):
        return "boot_auth_invalid"
    except Exception:
        return "boot_auth_invalid"
