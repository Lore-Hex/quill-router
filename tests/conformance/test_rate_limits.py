"""Shared counters must bound concurrent requests independently of sessions."""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from trusted_router.storage_models import RateLimitHit
from trusted_router.store_protocol import Store


def test_rate_limit_boundary_and_reset(store: Store, unique: str) -> None:
    now = dt.datetime(2026, 10, 17, 12, 0, 58, tzinfo=dt.UTC)

    def hit(at: dt.datetime) -> RateLimitHit:
        return store.hit_rate_limit(
            namespace="social_rewards_claim_v1", subject=unique,
            limit=2, window_seconds=60, now=at,
        )

    first, last, rejected = hit(now), hit(now), hit(now)
    assert first.allowed and first.remaining == 1
    assert last.allowed and last.remaining == 0
    assert not rejected.allowed and rejected.remaining == 0
    assert rejected.limit == 2
    assert rejected.reset_at == "2026-10-17T12:01:00Z"
    assert rejected.retry_after_seconds == 2
    reset = hit(now + dt.timedelta(seconds=2))
    assert reset.allowed and reset.remaining == 1
    assert reset.retry_after_seconds == 60


def test_rate_limit_subjects_and_namespaces_are_independent(store: Store, unique: str) -> None:
    now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC)

    def hit(namespace: str, subject: str) -> RateLimitHit:
        return store.hit_rate_limit(
            namespace=namespace, subject=subject, limit=1, window_seconds=60, now=now,
        )

    assert hit("social_rewards_claim_v1", unique).allowed
    assert not hit("social_rewards_claim_v1", unique).allowed
    assert hit("social_rewards_claim_v1", unique + "-other").allowed
    assert hit("other_campaign", unique).allowed


def test_rate_limit_concurrent_initial_requests_share_allowance(store: Store, unique: str) -> None:
    now = dt.datetime(2026, 10, 17, 12, tzinfo=dt.UTC)

    def hit(_: int) -> RateLimitHit:
        return store.hit_rate_limit(
            namespace="social_rewards_claim_v1", subject=unique,
            limit=5, window_seconds=60, now=now,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(hit, range(12)))
    assert sum(result.allowed for result in results) == 5
    assert sorted(result.remaining for result in results if result.allowed) == [0, 1, 2, 3, 4]
    assert not hit(0).allowed
