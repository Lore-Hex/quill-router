"""Portable storage semantics of the attested gateway boot registry.

Stage D heartbeats verify every request against the boot record registered
for the enclave's receipt key, so each backend must persist that record
durably and must never let a second registration silently replace an
existing kid's identity (jwk, image digest, attestation kind).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from trusted_router.gateway_boot import SpendLeaseBoot
from trusted_router.receipt_keys import b64url_encode
from trusted_router.store_protocol import Store


def _boot(unique: str) -> SpendLeaseBoot:
    public = hashlib.sha256(unique.encode()).digest()
    return SpendLeaseBoot(
        kid=b64url_encode(hashlib.sha256(public).digest()),
        jwk={"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public)},
        approved=True,
        verified=True,
        image_digest="sha256:" + "11" * 32,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-08-27T00:00:00Z",
    )


def test_gateway_boot_is_durable_and_refuses_a_conflicting_kid(
    store: Store,
    unique: str,
) -> None:
    boot = _boot(unique)
    assert store.get_spend_lease_boot(boot.kid) is None
    assert store.observe_spend_lease_boot(boot) == boot
    assert store.get_spend_lease_boot(boot.kid) == boot
    # Re-registering the same identity is idempotent.
    assert store.observe_spend_lease_boot(boot) == boot
    for change in (
        {"image_digest": "sha256:" + "22" * 32},
        {"attestation_kind": "aws"},
        {"jwk": {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(b"\x01" * 32)}},
    ):
        with pytest.raises(ValueError, match="kid collision"):
            store.observe_spend_lease_boot(replace(boot, **change))
    assert store.get_spend_lease_boot(boot.kid) == boot


def test_gateway_boot_registration_merges_verification_upward_only(
    store: Store,
    unique: str,
) -> None:
    # A boot first seen unverified (a non-GCP attestation) and later verified
    # keeps the stronger verdict; a later unverified sighting never downgrades it.
    unverified = replace(_boot(unique), approved=False, verified=False)
    assert store.observe_spend_lease_boot(unverified) == unverified
    verified = replace(unverified, approved=True, verified=True)
    assert store.observe_spend_lease_boot(verified) == verified
    assert store.observe_spend_lease_boot(unverified) == verified
    stored = store.get_spend_lease_boot(unverified.kid)
    assert stored is not None and stored.verified is True and stored.approved is True
