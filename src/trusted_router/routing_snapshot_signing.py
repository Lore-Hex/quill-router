"""Ed25519 manifest over a routing snapshot's bytes; no key IO.

The snapshot body (megabytes) exceeds detached_jws's 64 KiB token bound, so the
compact JWS signs a small manifest that binds the body's sha256 and size, and
repeats the body's window, revision and plane. A verifier hashes the bytes it
received and never re-serializes them. Ed25519, not HMAC: a shared key would
let every node that verifies a snapshot also forge one. Key custody (a pinned
Secret Manager version, rotation) is the publisher's
(services/routing_snapshot_publisher.py, docs/runbooks/routing-snapshot-key.md).

The same key signs the edge policy (``EdgePolicySigner``, E2 P2) under its
own header ``typ`` (``EDGE_POLICY_TYP``): each verifier refuses the other's
``typ`` before it reads a claim, so neither document can stand in for the
other.
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
TYP = "tr-routing-snapshot-v2"
# The golden files' key, derived from a public seed in
# scripts/fastpath/routing_snapshot_vectors.py. Anyone can sign with it, so
# signing and verification refuse it unless the caller asks for test mode.
TEST_KID = "routing-snapshot-test-key-1"
TEST_PUBLIC_KEY_B64URL = "U1I1REk6JqeFqKq5ZGH0P8b1b_OEwbFG0lCOkYL-jjY"
EDGE_POLICY_TYP = "tr-edge-policy-v1"
_KEY_FIELD = re.compile(r"[A-Za-z0-9_./:@+\-]{1,512}")


class ManifestClaims(Frozen):
    v: Literal[2]
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

    The body must parse as a v2 snapshot and be its own canonical encoding, so
    two encodings of one snapshot cannot carry two digests.
    """
    snapshot = parse_routing_snapshot(body)
    if canonical_snapshot_bytes(snapshot) != body:
        raise ValueError("snapshot body is not canonical")
    claims = {
        "v": 2,
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


def _is_test_key(key: TrustedKey) -> bool:
    return key.kid == TEST_KID or key.public_key_b64url == TEST_PUBLIC_KEY_B64URL


def _verified_claims(
    token: str, keys: Sequence[TrustedKey], *, expected_plane: Plane, expected_aud: str,
    allow_test_key: bool,
) -> tuple[ManifestClaims, TrustedKey, bytes]:
    claims, key, payload = verify(token, keys, TYP, PURPOSE)
    if _is_test_key(key) and not allow_test_key:
        raise ValueError("test key outside test mode")
    parsed = ManifestClaims.model_validate(claims)
    if payload != canonical(claims) or parsed.iss != key.iss or parsed.aud != key.aud:
        raise ValueError("manifest issuer or encoding")
    if parsed.aud != expected_aud:
        raise ValueError("manifest audience")
    if parsed.plane != expected_plane:
        raise ValueError("manifest plane")
    return parsed, key, payload


def verify_manifest(
    token: str, keys: Sequence[TrustedKey], body: bytes, *, now: int,
    expected_plane: Plane, expected_aud: str, allow_test_key: bool = False,
) -> ManifestClaims:
    """Verify signature, purpose, issuer, audience, plane, the body binding and freshness.

    The plane and audience are the verifier's own: a snapshot signed for
    another plane or another consumer is refused even under a trusted key.
    """
    parsed, key, payload = _verified_claims(
        token, keys, expected_plane=expected_plane, expected_aud=expected_aud,
        allow_test_key=allow_test_key,
    )
    if canonical(manifest_claims(body, iss=key.iss, aud=key.aud)) != payload:
        raise ValueError("manifest binding")
    if not parsed.compiled_at <= now < parsed.not_after:
        raise ValueError("snapshot freshness")
    return parsed


def verify_manifest_digest(
    token: str, keys: Sequence[TrustedKey], body: bytes, *,
    expected_plane: Plane, expected_aud: str, allow_test_key: bool = False,
) -> ManifestClaims:
    """``verify_manifest``'s signature, key and claim checks, and the body's
    sha256 and size, without parsing the body or checking freshness.

    For a body whose signer already checked it in full (the publisher's
    worker, whose ``RoutingSnapshotSigner.sign`` verifies what it signs): the
    process that serves it checks the bytes it holds are the ones signed under
    its own key, plane and audience, at a fraction of a full parse's cost.
    """
    parsed, _key, _payload = _verified_claims(
        token, keys, expected_plane=expected_plane, expected_aud=expected_aud,
        allow_test_key=allow_test_key,
    )
    if parsed.sha256 != hashlib.sha256(body).hexdigest() or parsed.size != len(body):
        raise ValueError("manifest binding")
    return parsed


class RoutingSnapshotSigner:
    """Signs manifests with a key reserved for this purpose alone.

    ``reserved`` lists the trusted keys of every other signing purpose the
    process holds (async settle tickets, speculation shadow, ...): the routing
    key must share neither a kid nor a public key with any of them.
    """

    def __init__(
        self, private: Ed25519PrivateKey, trusted: TrustedKey,
        *, reserved: Sequence[TrustedKey] = (), allow_test_key: bool = False,
    ) -> None:
        public = b64encode(private.public_key().public_bytes_raw())
        if (trusted.purpose != PURPOSE or not all((trusted.kid, trusted.iss, trusted.aud))
                or public != trusted.public_key_b64url
                or any(_KEY_FIELD.fullmatch(v) is None
                       for v in (trusted.kid, trusted.iss, trusted.aud))):
            raise ValueError("routing snapshot key configuration")
        if _is_test_key(trusted) and not allow_test_key:
            raise ValueError("test key outside test mode")
        for other in reserved:
            if other.kid == trusted.kid:
                raise ValueError("separate kid required")
            if other.public_key_b64url == public:
                raise ValueError("separate key required")
        self.private, self.trusted, self.allow_test_key = private, trusted, allow_test_key

    def sign(self, body: bytes) -> str:
        claims = manifest_claims(body, iss=self.trusted.iss, aud=self.trusted.aud)
        header = {"alg": "EdDSA", "kid": self.trusted.kid, "typ": TYP}
        material = b64encode(canonical(header)) + "." + b64encode(canonical(claims))
        token = material + "." + b64encode(self.private.sign(material.encode("ascii")))
        verify_manifest(
            token, [self.trusted], body, now=claims["compiled_at"],
            expected_plane=Plane.model_validate(claims["plane"]),
            expected_aud=self.trusted.aud, allow_test_key=self.allow_test_key,
        )
        return token


# --- the edge policy (E2 P2) -------------------------------------------------------------------


class EdgePolicyManifestClaims(Frozen):
    v: Literal[1]
    sha256: Digest
    size: UInt
    issued_at: UInt
    not_after: UInt
    plane: Plane
    iss: Identity
    aud: Identity


def edge_policy_manifest_claims(body: bytes, *, iss: str, aud: str) -> dict[str, Any]:
    """The claims an edge-policy manifest must carry for exactly these bytes.

    docs/design/fast-admission-e2-decide.md section 4. The manifest is a
    compact JWS (Ed25519, the routing-snapshot key, purpose
    ``routing-snapshot``, issuer and audience the key's) whose header is
    exactly ``{"alg":"EdDSA","kid":<kid>,"typ":"tr-edge-policy-v1"}`` and
    whose payload is exactly these claims, both canonical (sorted keys,
    compact, ASCII), each base64url without padding:

        {"aud", "iss", "issued_at", "not_after",
         "plane": {"cloud", "region"}, "sha256", "size", "v": 1}

    ``sha256`` (lowercase hex) and ``size`` bind the body bytes;
    ``issued_at``, ``not_after`` and ``plane`` repeat the body's. The body
    must parse as an edge policy v1 (``v`` 1, ``kind`` ``tr-edge-policy``)
    and be its own canonical encoding (trusted_router/edge_policy.py). A verifier checks the signature, the
    ``typ``, the key's purpose, that ``iss`` and ``aud`` are the key's and
    ``aud`` its own, that ``plane`` is its own, that the claims are exactly
    the ones the bytes it received imply, and ``issued_at <= now <
    not_after``.
    """
    # Imported here: the routing snapshot's signer loads no edge-policy module.
    from trusted_router.edge_policy import canonical_edge_policy_bytes, parse_edge_policy

    policy = parse_edge_policy(body)
    if canonical_edge_policy_bytes(policy) != body:
        raise ValueError("edge policy body is not canonical")
    claims = {
        "v": 1,
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "issued_at": policy.issued_at,
        "not_after": policy.not_after,
        "plane": policy.plane.model_dump(mode="json"),
        "iss": iss,
        "aud": aud,
    }
    EdgePolicyManifestClaims.model_validate(claims)
    return claims


def verify_edge_policy_manifest(
    token: str, keys: Sequence[TrustedKey], body: bytes, *, now: int,
    expected_plane: Plane, expected_aud: str, allow_test_key: bool = False,
) -> EdgePolicyManifestClaims:
    """``verify_manifest`` for the edge policy: its own ``typ``, claims and body.

    A routing-snapshot manifest is refused here (``type``), as an edge-policy
    manifest is refused by ``verify_manifest``.
    """
    claims, key, payload = verify(token, keys, EDGE_POLICY_TYP, PURPOSE)
    if _is_test_key(key) and not allow_test_key:
        raise ValueError("test key outside test mode")
    parsed = EdgePolicyManifestClaims.model_validate(claims)
    if payload != canonical(claims) or parsed.iss != key.iss or parsed.aud != key.aud:
        raise ValueError("manifest issuer or encoding")
    if parsed.aud != expected_aud:
        raise ValueError("manifest audience")
    if parsed.plane != expected_plane:
        raise ValueError("manifest plane")
    if canonical(edge_policy_manifest_claims(body, iss=key.iss, aud=key.aud)) != payload:
        raise ValueError("manifest binding")
    if not parsed.issued_at <= now < parsed.not_after:
        raise ValueError("edge policy freshness")
    return parsed


class EdgePolicySigner:
    """Signs edge-policy manifests with the routing snapshot's key.

    Built from a ``RoutingSnapshotSigner``, so the key carries every check that
    signer makes (pinned purpose, separate from every other purpose's key,
    the test key only in test mode); only the ``typ`` and the claims differ.
    """

    def __init__(self, signer: RoutingSnapshotSigner) -> None:
        self.private, self.trusted = signer.private, signer.trusted
        self.allow_test_key = signer.allow_test_key

    def sign(self, body: bytes) -> str:
        claims = edge_policy_manifest_claims(body, iss=self.trusted.iss, aud=self.trusted.aud)
        header = {"alg": "EdDSA", "kid": self.trusted.kid, "typ": EDGE_POLICY_TYP}
        material = b64encode(canonical(header)) + "." + b64encode(canonical(claims))
        token = material + "." + b64encode(self.private.sign(material.encode("ascii")))
        verify_edge_policy_manifest(
            token, [self.trusted], body, now=claims["issued_at"],
            expected_plane=Plane.model_validate(claims["plane"]),
            expected_aud=self.trusted.aud, allow_test_key=self.allow_test_key,
        )
        return token
