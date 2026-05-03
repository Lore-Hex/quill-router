"""Concurrent-access tests for the per-feature in-memory stores.

Wallet challenges, verification tokens, and SNS message-id replay are
all "exactly-once" gates: two threads racing on the same nonce / token /
message_id must not both succeed. The single-threaded contract tests
exercise the happy path; these prove the locks hold under contention.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from trusted_router.storage_email_blocks import InMemoryEmailBlocks
from trusted_router.storage_verification_tokens import InMemoryVerificationTokens
from trusted_router.storage_wallet_challenges import InMemoryWalletChallenges


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


def test_distinct_nonces_can_be_consumed_concurrently() -> None:
    """Negative test for the lock: distinct keys must NOT serialize via
    the shared lock to the point of one starving another. We don't
    measure timing — just confirm both succeed."""
    store = InMemoryWalletChallenges()
    address = "0x" + "b" * 40
    nonces = [
        store.create(address=address, message=f"m{i}", ttl_seconds=300)[0]
        for i in range(32)
    ]

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda n: store.consume(n), nonces))
    assert all(r is not None for r in results)


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
