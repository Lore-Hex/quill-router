"""The reward endpoint's shared account throttle survives session rotation."""

import datetime as dt

from tests.test_social_rewards import _account, _post, _settings
from trusted_router.social_rewards import ACTIONS
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.typed_balance import live_credit_summary


def test_reward_throttle_survives_new_session_and_generic_limiter_disabled(monkeypatch):
    original_hit = InMemoryStore.hit_rate_limit

    def same_minute(self, **kwargs):
        return original_hit(self, **{**kwargs, "now": dt.datetime(2026, 10, 17, tzinfo=dt.UTC)})

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", same_minute)
    client, result, _ = _account(_settings(
        social_rewards_claims_per_minute=5, rate_limit_enabled=False,
    ), activated=False)
    for action, _, _ in ACTIONS:
        assert _post(client, action=action).status_code == 200
    token, _ = STORE.create_auth_session(
        user_id=result.user.id, workspace_id=result.workspace.id,
        provider="google", label="new-session", ttl_seconds=3600,
    )
    client.cookies.set("tr_session", token)
    rejected = _post(client, action="github")
    assert rejected.status_code == 429
    assert 1 <= int(rejected.headers["Retry-After"]) <= 60
    assert live_credit_summary(result.workspace.id)["total_credits"] == 300_000
    # Read-only reconciliation stays available after the request quota is spent.
    status = client.get("/console/social-rewards")
    assert status.status_code == 200
    assert all(action["claimed"] for action in status.json()["actions"])


def test_reward_limiter_failure_cannot_invoke_credit_grant(monkeypatch):
    client, result, _ = _account(activated=False)
    granted = []

    def unavailable(self, **kwargs):
        raise TimeoutError("counter unavailable")

    def unexpected_credit(self, *args, **kwargs):
        granted.append(args)
        raise AssertionError("grant called after failed admission")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", unavailable)
    monkeypatch.setattr(InMemoryStore, "credit_workspace_once", unexpected_credit)
    response = _post(client, action="github")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert granted == []
    assert live_credit_summary(result.workspace.id)["total_credits"] == 150_000
