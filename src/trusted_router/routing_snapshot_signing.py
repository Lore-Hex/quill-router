"""Dormant Ed25519 manifest over a routing snapshot's bytes; no key IO, no runtime imports.

The snapshot body (megabytes) exceeds detached_jws's 64 KiB token bound, so the
compact JWS signs a small manifest that binds the body's sha256 and size, and
repeats the body's window, revision and plane. A verifier hashes the bytes it
received and never re-serializes them. Ed25519, not HMAC: a shared key would
let every node that verifies a snapshot also forge one. Key custody (pinned
Secret Manager versions, rotation) belongs to the publishing PR.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.billing_snapshot import Digest, Frozen, Identity, UInt
from trusted_router.detached_jws import TrustedKey, b64encode, canonical, verify
from trusted_router.routing_snapshot import (
    Plane,
    canonical_snapshot_bytes,
    parse_routing_snapshot,
)

PURPOSE = "routing-snapshot"
TYP = "tr-routing-snapshot-v1"
_KEY_FIELD = re.compile(r"[A-Za-z0-9_./:@+\-]{1,512}")


class ManifestClaims(Frozen):
    v: Literal[1]
    sha256: Digest
    size: UInt
    compiled_at: UInt
    not_after: UInt
    source_revision: Identity
    plane: Plane
    iss: Identity
    aud: Identity


def manifest_claims(body: bytes, *, iss: str, aud: str) -> dict[str, Any]:
    """The claims a manifest must carry for exactly these bytes.

    The body must parse as a v1 snapshot and be its own canonical encoding, so
    two encodings of one snapshot cannot carry two digests.
    """
    snapshot = parse_routing_snapshot(body)
    if canonical_snapshot_bytes(snapshot) != body:
        raise ValueError("snapshot body is not canonical")
    claims = {
        "v": 1,
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "compiled_at": snapshot.compiled_at,
        "not_after": snapshot.not_after,
        "source_revision": snapshot.source_revision,
        "plane": snapshot.plane.model_dump(mode="json"),
        "iss": iss,
        "aud": aud,
    }
    ManifestClaims.model_validate(claims)
    return claims


def verify_manifest(
    token: str, keys: Sequence[TrustedKey], body: bytes, *, now: int,
) -> ManifestClaims:
    """Verify signature, purpose, issuer, the body binding and freshness."""
    claims, key, payload = verify(token, keys, TYP, PURPOSE)
    parsed = ManifestClaims.model_validate(claims)
    if payload != canonical(claims) or parsed.iss != key.iss or parsed.aud != key.aud:
        raise ValueError("manifest issuer or encoding")
    if canonical(manifest_claims(body, iss=key.iss, aud=key.aud)) != payload:
        raise ValueError("manifest binding")
    if not parsed.compiled_at <= now < parsed.not_after:
        raise ValueError("snapshot freshness")
    return parsed


class RoutingSnapshotSigner:
    """Signs manifests with a key reserved for this purpose alone.

    ``reserved`` lists the trusted keys of every other signing purpose the
    process holds (async settle tickets, speculation shadow, ...): the routing
    key must share neither a kid nor a public key with any of them.
    """

    def __init__(
        self, private: Ed25519PrivateKey, trusted: TrustedKey,
        *, reserved: Sequence[TrustedKey] = (),
    ) -> None:
        public = b64encode(private.public_key().public_bytes_raw())
        if (trusted.purpose != PURPOSE or not all((trusted.kid, trusted.iss, trusted.aud))
                or public != trusted.public_key_b64url
                or any(_KEY_FIELD.fullmatch(v) is None
                       for v in (trusted.kid, trusted.iss, trusted.aud))):
            raise ValueError("routing snapshot key configuration")
        for other in reserved:
            if other.kid == trusted.kid:
                raise ValueError("separate kid required")
            if other.public_key_b64url == public:
                raise ValueError("separate key required")
        self.private, self.trusted = private, trusted

    def sign(self, body: bytes) -> str:
        claims = manifest_claims(body, iss=self.trusted.iss, aud=self.trusted.aud)
        header = {"alg": "EdDSA", "kid": self.trusted.kid, "typ": TYP}
        material = b64encode(canonical(header)) + "." + b64encode(canonical(claims))
        token = material + "." + b64encode(self.private.sign(material.encode("ascii")))
        verify_manifest(token, [self.trusted], body, now=claims["compiled_at"])
        return token
