"""Portable Stage A spend-lease storage semantics."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from trusted_router.receipt_keys import b64url_encode
from trusted_router.spend_leases import (
    SpendLeaseBoot,
    SpendLeaseSigner,
    mint_shadow_spend_lease,
)
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


def test_spend_lease_boot_and_generation_are_durable_and_monotonic(
    store: Store,
    unique: str,
) -> None:
    boot = _boot(unique)
    assert store.observe_spend_lease_boot(boot) == boot
    assert store.get_spend_lease_boot(boot.kid) == boot
    assert store.next_spend_lease_generation(f"key-{unique}", boot.kid) == 1
    assert store.next_spend_lease_generation(f"key-{unique}", boot.kid) == 2
    assert store.next_spend_lease_generation(f"other-{unique}", boot.kid) == 1
    with pytest.raises(ValueError, match="kid collision"):
        store.observe_spend_lease_boot(
            replace(boot, image_digest="sha256:" + "22" * 32)
        )


def test_gateway_authorization_replay_preserves_verbatim_spend_lease_token(
    store: Store,
    workspace_id: str,
    unique: str,
) -> None:
    artifact = mint_shadow_spend_lease(
        signer=SpendLeaseSigner(lambda: bytes(range(32))),
        key_hash=f"lease-key-{unique}",
        workspace_id=workspace_id,
        boot_kid=_boot(unique).kid,
        cap_micro=1_000_000,
        gen=1,
        catalog={"version": "catalog-v1", "candidates": []},
        ttl_seconds=60,
        now=1_700_000_000,
    )
    kwargs = {
        "workspace_id": workspace_id,
        "key_hash": f"lease-key-{unique}",
        "model_id": "vendor/model",
        "provider": "vendor",
        "usage_type": "Credits",
        "estimated_microdollars": 100,
        "credit_reservation_id": None,
        "idempotency_key": f"lease-idem-{unique}",
        "spend_lease": artifact,
    }
    first = store.create_gateway_authorization(**kwargs)  # type: ignore[arg-type]
    replay = store.create_gateway_authorization(**kwargs)  # type: ignore[arg-type]
    fetched = store.get_gateway_authorization(first.id)
    assert replay.id == first.id
    assert replay.spend_lease_token == artifact.token
    assert fetched is not None
    assert fetched.spend_lease_token == artifact.token
    assert fetched.spend_lease_status == "active"
    assert fetched.spend_lease_gen == 1


def test_active_spend_lease_is_retained_until_strictly_newer_replacement(
    store: Store,
    workspace_id: str,
    unique: str,
) -> None:
    key_hash = f"active-lease-key-{unique}"
    boot_kid = _boot(unique).kid
    first = mint_shadow_spend_lease(
        signer=SpendLeaseSigner(lambda: bytes(range(32))),
        key_hash=key_hash,
        workspace_id=workspace_id,
        boot_kid=boot_kid,
        cap_micro=1_000_000,
        gen=1,
        catalog={"version": "catalog-v1", "candidates": []},
        ttl_seconds=60,
        now=1_700_000_000,
    )
    older = replace(first, token="older", lease_id="older", gen=0)  # noqa: S106
    newer = replace(first, token="newer", lease_id="newer", gen=2)  # noqa: S106

    assert store.get_active_spend_lease(key_hash, boot_kid) is None
    assert store.retain_spend_lease(key_hash, boot_kid, first, replace=False) == first
    assert store.retain_spend_lease(key_hash, boot_kid, newer, replace=False) == first
    assert store.retain_spend_lease(key_hash, boot_kid, older, replace=True) == first
    assert store.retain_spend_lease(key_hash, boot_kid, newer, replace=True) == newer
    assert store.get_active_spend_lease(key_hash, boot_kid) == newer
