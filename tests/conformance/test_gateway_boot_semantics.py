"""Portable storage semantics of the attested gateway boot registry.

Stage D heartbeats verify every request against the boot record registered
for the enclave's receipt key, so each backend must persist that record
durably and must never let a second registration silently replace an
existing kid's identity (jwk, image digest, attestation kind).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime

import pytest

from trusted_router.gateway_boot import GatewayBoot
from trusted_router.receipt_keys import b64url_encode
from trusted_router.storage_models import BOOT_DECLARED_AT_ITS_WRITE
from trusted_router.store_protocol import Store


def _boot(unique: str) -> GatewayBoot:
    public = hashlib.sha256(unique.encode()).digest()
    return GatewayBoot(
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
    assert store.get_gateway_boot(boot.kid) is None
    assert store.observe_gateway_boot(boot) == boot
    assert store.get_gateway_boot(boot.kid) == boot
    # Re-registering the same identity is idempotent.
    assert store.observe_gateway_boot(boot) == boot
    for change in (
        {"image_digest": "sha256:" + "22" * 32},
        {"attestation_kind": "aws"},
        {"jwk": {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(b"\x01" * 32)}},
    ):
        with pytest.raises(ValueError, match="kid collision"):
            store.observe_gateway_boot(replace(boot, **change))
    assert store.get_gateway_boot(boot.kid) == boot


def test_gateway_boot_registration_merges_verification_upward_only(
    store: Store,
    unique: str,
) -> None:
    # A boot first seen unverified (a non-GCP attestation) and later verified
    # keeps the stronger verdict; a later unverified sighting never downgrades it.
    unverified = replace(_boot(unique), approved=False, verified=False)
    assert store.observe_gateway_boot(unverified) == unverified
    verified = replace(unverified, approved=True, verified=True)
    assert store.observe_gateway_boot(verified) == verified
    assert store.observe_gateway_boot(unverified) == verified
    stored = store.get_gateway_boot(unverified.kid)
    assert stored is not None and stored.verified is True and stored.approved is True


def test_gateway_boot_declarations_follow_the_latest_registration(
    store: Store,
    unique: str,
) -> None:
    # Unlike verification, a declaration is not merged upward: it is whatever
    # the latest registration sent, so a boot can withdraw it, and it is read
    # back exactly on every backend.
    undeclared = _boot(unique)
    assert undeclared.declares_usage_heartbeat is False
    assert undeclared.declares_stream_open_heartbeat is False
    assert undeclared.declares_idempotency_key_source is False
    assert undeclared.declares_settle_outbox is False
    assert store.observe_gateway_boot(undeclared) == undeclared

    declared = replace(
        undeclared,
        declares_usage_heartbeat=True,
        declares_stream_open_heartbeat=True,
        declares_idempotency_key_source=True,
        declares_settle_outbox=True,
    )
    # A registration that declares usage heartbeats is also stored with when
    # that declaration began, which is the store's to say (the test below).
    assert _declarations(store.observe_gateway_boot(declared)) == declared
    assert _declarations(store.get_gateway_boot(declared.kid)) == declared

    partly = replace(undeclared, declares_usage_heartbeat=True)
    assert _declarations(store.observe_gateway_boot(partly)) == partly
    assert _declarations(store.get_gateway_boot(declared.kid)) == partly

    # E4 R2: each of the two new declarations is replaced on its own too.
    outbox_only = replace(undeclared, declares_settle_outbox=True)
    assert store.observe_gateway_boot(outbox_only) == outbox_only
    assert store.get_gateway_boot(declared.kid) == outbox_only
    key_source_only = replace(undeclared, declares_idempotency_key_source=True)
    assert store.observe_gateway_boot(key_source_only) == key_source_only
    assert store.get_gateway_boot(declared.kid) == key_source_only

    assert store.observe_gateway_boot(undeclared) == undeclared
    assert store.get_gateway_boot(declared.kid) == undeclared


def _declarations(boot: GatewayBoot | None) -> GatewayBoot | None:
    """The record without the store's own note of when the declaration began."""
    return None if boot is None else replace(boot, usage_heartbeat_declared_since=None)


def test_gateway_boot_keeps_when_its_usage_heartbeat_declaration_began(
    store: Store,
    unique: str,
) -> None:
    """The early release of an unstarted hold must know that a boot was
    declaring usage heartbeats when an authorization was made, not only that
    it declares them now. Every backend keeps the field well-formed: None
    while the latest registration does not declare, otherwise the marker for
    "this stored row's own write" or a time the store itself read; whatever
    a caller sends, in the field or as its registration time, is never
    taken. A backend that runs the rule (Spanner) resolves it to its own
    commit time: kept across registrations that keep declaring, gone when
    one does not, and starting again, no earlier, when the declaration
    returns."""
    sent = "2020-01-01T00:00:00Z"
    resolve = getattr(store, "gateway_boot_declaring_since", None)
    first = replace(_boot(unique), declares_usage_heartbeat=True, registered_at="2026-08-27T00:00:00Z")
    stored = store.observe_gateway_boot(replace(first, usage_heartbeat_declared_since=sent))
    assert stored.usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    began = resolve(first.kid) if callable(resolve) else None

    again = replace(first, registered_at="2026-08-27T01:00:00Z", usage_heartbeat_declared_since=sent)
    stored = store.observe_gateway_boot(again)
    assert stored.registered_at == "2026-08-27T00:00:00Z"
    carried = stored.usage_heartbeat_declared_since
    assert carried is not None and carried not in (sent, again.registered_at, first.registered_at)
    read = store.get_gateway_boot(first.kid)
    assert read is not None and read.usage_heartbeat_declared_since == carried
    if callable(resolve):
        # The first row's commit time, read by the registration that replaced it.
        assert began is not None and began.tzinfo is not None
        assert carried != BOOT_DECLARED_AT_ITS_WRITE
        assert datetime.fromisoformat(carried.replace("Z", "+00:00")) == began
        assert resolve(first.kid) == began
        store.observe_gateway_boot(again)
        assert resolve(first.kid) == began
    else:
        # A store that keeps no commit time never carries a start.
        assert carried == BOOT_DECLARED_AT_ITS_WRITE

    withdrawn = replace(first, declares_usage_heartbeat=False, registered_at="2026-08-27T02:00:00Z")
    assert store.observe_gateway_boot(withdrawn).usage_heartbeat_declared_since is None
    read = store.get_gateway_boot(first.kid)
    assert read is not None and read.declares_usage_heartbeat is False
    assert read.usage_heartbeat_declared_since is None
    if callable(resolve):
        assert resolve(first.kid) is None

    restored = replace(first, registered_at="2026-08-27T03:00:00Z", usage_heartbeat_declared_since=sent)
    assert store.observe_gateway_boot(restored).usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    read = store.get_gateway_boot(first.kid)
    assert read is not None and read.usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    if callable(resolve):
        again_began = resolve(first.kid)
        assert again_began is not None and began is not None and again_began >= began

    # A first registration that does not declare has no start, whatever it sends.
    other = replace(_boot(unique + "-other"), usage_heartbeat_declared_since=sent)
    assert store.observe_gateway_boot(other).usage_heartbeat_declared_since is None
    if callable(resolve):
        assert resolve(other.kid) is None and resolve("no-such-boot") is None
