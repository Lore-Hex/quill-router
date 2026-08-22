"""Concurrent-access tests for the per-feature in-memory stores.

Wallet challenges, verification tokens, and SNS message-id replay are
all "exactly-once" gates: two threads racing on the same nonce / token /
message_id must not both succeed. The single-threaded contract tests
exercise the happy path; these prove the locks hold under contention.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from trusted_router.storage_email_blocks import InMemoryEmailBlocks
from trusted_router.storage_verification_tokens import InMemoryVerificationTokens
from trusted_router.storage_wallet_challenges import InMemoryWalletChallenges


def test_wallet_challenge_rejects_mismatched_canonical_message_nonce() -> None:
    store = InMemoryWalletChallenges()

    with pytest.raises(ValueError, match="does not match"):
        store.create(
            address="0x" + "a" * 40,
            message=(
                "trusted.example wants you to sign in with your Ethereum account:\n"
                f"{'0x' + 'a' * 40}\n\nNonce: nonce-in-message"
            ),
            ttl_seconds=300,
            raw_nonce="different-raw-nonce",
        )

    assert store._challenges == {}
    assert store._ids_by_lookup_hash == {}
    assert store._ids_by_scope == {}


def test_consume_wallet_challenge_is_exactly_once_under_contention() -> None:
    """Two threads racing the same nonce — at most one wins. The other
    sees None. Lock must serialize the read-then-mark-consumed pattern."""
    store = InMemoryWalletChallenges()
    address = "0x" + "a" * 40
    raw_nonce, _ = store.create(
        address=address,
        message="signed-message",
        ttl_seconds=300,
    )

    def attempt() -> bool:
        return store.consume(raw_nonce) is not None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: attempt(), range(64)))
    # Exactly one consumer ever sees the record.
    assert results.count(True) == 1
    assert results.count(False) == 63


def test_consume_verification_token_is_exactly_once_under_contention() -> None:
    store = InMemoryVerificationTokens()
    raw_token, _ = store.create(user_id="u-1", purpose="signup", ttl_seconds=300)

    def attempt() -> bool:
        return store.consume(raw_token, purpose="signup") is not None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: attempt(), range(64)))
    assert results.count(True) == 1
    assert results.count(False) == 63


def test_record_sns_message_once_returns_true_only_to_first_caller() -> None:
    """SNS may redeliver the same message_id multiple times; the dedupe
    gate must still produce exactly one True."""
    store = InMemoryEmailBlocks()

    def attempt() -> bool:
        return store.record_message_once("msg-shared")

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: attempt(), range(128)))
    assert results.count(True) == 1


def test_distinct_wallets_can_be_consumed_concurrently() -> None:
    """The newest-only rule is scoped to an address, not the entire store."""
    store = InMemoryWalletChallenges()
    nonces = [
        store.create(
            address=f"wallet-{i}",
            message=f"m{i}",
            ttl_seconds=300,
        )[0]
        for i in range(32)
    ]

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda n: store.consume(n), nonces))
    assert all(r is not None for r in results)


def test_repeated_wallet_issuance_reuses_active_nonce_with_constant_memory() -> None:
    """Catch mutations that append or replace a still-live challenge."""
    store = InMemoryWalletChallenges()
    address = "0x" + "b" * 40
    domain = "trusted.example"
    issued = [
        store.create(
            address=address.upper() if i % 2 else f"  {address}  ",
            message=(
                f"{domain} wants you to sign in with your Ethereum account:\n"
                f"{address}\n\nNonce: nonce-{i}"
            ),
            ttl_seconds=300,
            raw_nonce=f"nonce-{i}",
        )
        for i in range(1_000)
    ]

    assert len(store._challenges) == 1
    assert len(store._ids_by_lookup_hash) == 1
    assert len(store._ids_by_scope) == 1
    assert {nonce for nonce, _record in issued} == {"nonce-0"}
    assert {record.hash for _nonce, record in issued} == {issued[0][1].hash}
    assert store.consume("nonce-999") is None
    active = store.consume("nonce-0")
    assert active is not None
    assert "Nonce: nonce-0" in active.message

    fresh_nonce, fresh = store.create(
        address=address,
        message=(
            f"{domain} wants you to sign in with your Ethereum account:\n"
            f"{address}\n\nNonce: nonce-after-consume"
        ),
        ttl_seconds=300,
        raw_nonce="nonce-after-consume",
    )
    assert fresh_nonce == "nonce-after-consume"
    assert fresh.hash != active.hash

    fresh.expires_at = "2000-01-01T00:00:00Z"
    after_expiry_nonce, after_expiry = store.create(
        address=address,
        message=(
            f"{domain} wants you to sign in with your Ethereum account:\n"
            f"{address}\n\nNonce: nonce-after-expiry"
        ),
        ttl_seconds=300,
        raw_nonce="nonce-after-expiry",
    )
    assert after_expiry_nonce == "nonce-after-expiry"
    assert after_expiry.hash != fresh.hash


def test_concurrent_wallet_issuance_leaves_exactly_one_live_nonce() -> None:
    store = InMemoryWalletChallenges()
    address = "0x" + "d" * 40

    def issue(index: int) -> str:
        return store.create(
            address=address,
            message=(
                "trusted.example wants you to sign in with your Ethereum account:\n"
                f"{address}\n\nNonce: concurrent-nonce-{index}"
            ),
            ttl_seconds=300,
            raw_nonce=f"concurrent-nonce-{index}",
        )[0]

    with ThreadPoolExecutor(max_workers=16) as ex:
        nonces = list(ex.map(issue, range(128)))
    assert len(set(nonces)) == 1
    with ThreadPoolExecutor(max_workers=16) as ex:
        consumed = list(ex.map(store.consume, nonces))

    assert sum(record is not None for record in consumed) == 1
    assert len(store._challenges) == 1
    assert len(store._ids_by_lookup_hash) == 1
    assert len(store._ids_by_scope) == 1


def test_block_email_then_concurrent_is_blocked_query() -> None:
    """Concurrent reads of `is_blocked` against an entry being written
    must not crash and must eventually all see the block."""
    store = InMemoryEmailBlocks()
    store.block(email="bouncer@example.com", reason="bounce")

    def query() -> bool:
        return store.is_blocked("bouncer@example.com")

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda _: query(), range(256)))
    assert all(results)


def test_replayed_consume_after_success_returns_none() -> None:
    """Once a wallet challenge is consumed, a follow-up consume by any
    caller (same or different thread) sees None — even if the second
    call arrives microseconds later."""
    store = InMemoryWalletChallenges()
    raw_nonce, _ = store.create(
        address="0x" + "c" * 40,
        message="msg",
        ttl_seconds=300,
    )
    first = store.consume(raw_nonce)
    second = store.consume(raw_nonce)
    assert first is not None
    assert second is None
