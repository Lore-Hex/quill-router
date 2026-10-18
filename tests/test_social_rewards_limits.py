"""The shared user quota cannot be reset by sessions or bypassed on store failure."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from trusted_router.config import Settings
from trusted_router.social_rewards_limits import enforce_social_reward_rate_limit
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import RateLimitHit


def test_default_allows_five_actions_and_retries_then_throttles():
    settings = Settings()
    for _ in range(30):
        enforce_social_reward_rate_limit("user-a", settings)
    with pytest.raises(HTTPException) as exc:
        enforce_social_reward_rate_limit("user-a", settings)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) >= 1
    enforce_social_reward_rate_limit("user-b", settings)


def test_shared_store_receives_user_identity_not_session_or_ip(monkeypatch):
    calls = []

    def hit(self, **kwargs):
        calls.append(kwargs)
        return RateLimitHit(True, 30, 29, "2026-10-17T00:01:00Z", 60)

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", hit)
    for _ in range(2):
        enforce_social_reward_rate_limit("user-stable", Settings())
    assert calls[0] == calls[1]
    assert calls[0]["namespace"] == "social_rewards_claim_v1"
    assert calls[0]["window_seconds"] == 60
    assert calls[0]["subject"] != "user-stable"


def test_unavailable_shared_limiter_fails_closed(monkeypatch):
    def unavailable(self, **kwargs):
        raise TimeoutError("private storage details")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", unavailable)
    with pytest.raises(HTTPException) as exc:
        enforce_social_reward_rate_limit("user-a", Settings())
    assert exc.value.status_code == 503
    assert exc.value.headers["Retry-After"] == "5"
    assert "private" not in exc.value.detail


def test_general_rate_limit_switch_does_not_disable_campaign_admission():
    settings = Settings(rate_limit_enabled=False, social_rewards_claims_per_minute=5)
    for _ in range(5):
        enforce_social_reward_rate_limit("user-a", settings)
    with pytest.raises(HTTPException) as exc:
        enforce_social_reward_rate_limit("user-a", settings)
    assert exc.value.status_code == 429
