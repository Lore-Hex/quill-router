"""Authorization, cohort boundaries and uncertain-commit recovery for rewards."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from trusted_router.acquisition import record_successful_api_call
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.oauth_provider import OAUTH_PROVIDERS, OAuthUserInfo
from trusted_router.routes.oauth import _persist_login
from trusted_router.social_rewards import ACTIONS, campaign_enabled, claim
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import AcquisitionAttribution
from trusted_router.typed_balance import live_credit_summary


def _settings(**overrides) -> Settings:
    return Settings(**{
        "environment": "test", "social_rewards_enabled": True,
        "social_rewards_plane_id": "gcp-primary", "rate_limit_enabled": False,
        "social_rewards_youtube_url": "https://www.youtube.com/channel/test-fixture",
        **overrides,
    })


def _account(settings=None, *, enrolled=True, activated=True):
    settings = settings or _settings()
    app = create_app(settings, init_observability=False)
    client = TestClient(app, raise_server_exceptions=False)
    result = STORE.signup(
        email="rewards@gmail.com", trial_credit_microdollars=150_000 if enrolled else 300_000,
        social_rewards_issuer="gcp-primary" if enrolled else None,
    )
    assert result is not None
    STORE.create_acquisition_attribution(AcquisitionAttribution(
        workspace_id=result.workspace.id, anonymous_id="", first_touch={}, last_touch={},
        signup_provider="google", starter_credit_microdollars=result.trial_credit_microdollars,
    ))
    if activated:
        record_successful_api_call(result.workspace.id, model="test", provider="test")
    token, _ = STORE.create_auth_session(
        user_id=result.user.id, workspace_id=result.workspace.id,
        provider="google", label="test", ttl_seconds=3600,
    )
    client.cookies.set("tr_session", token)
    return client, result, settings


def _post(client, path="claim", **body):
    return client.post(f"/console/social-rewards/{path}", json=body,
                       headers={"Origin": "http://testserver"})


def test_claim_before_first_call_is_committed_once():
    client, result, _ = _account(activated=False)
    response = _post(client, action="github", attested=True)
    assert response.status_code == 200
    assert response.json()["first_call_complete"] is False
    assert response.json()["actions"][0]["claimed"] is True
    assert _post(client, action="github", attested=True).status_code == 200
    assert live_credit_summary(result.workspace.id)["total_credits"] == 180_000


def test_claims_concurrent_fixed_total_and_destination():
    client, result, settings = _account(activated=False)
    other = STORE.create_workspace(result.user.id, "Other")
    other_initial_credit = live_credit_summary(other.id)["total_credits"]
    token, _ = STORE.create_auth_session(user_id=result.user.id, provider="google", label="other",
                                         workspace_id=other.id, ttl_seconds=3600)
    client.cookies.set("tr_session", token)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda a: claim(result.user.id, a, settings),
                      [action for action, _, _ in ACTIONS] * 4))
    assert live_credit_summary(result.workspace.id)["total_credits"] == 300_000
    assert live_credit_summary(other.id)["total_credits"] == other_initial_credit
    assert all(a["claimed"] for a in client.get("/console/social-rewards").json()["actions"])


def test_uncertain_commit_is_reconciled_and_retry_does_not_pay_twice(monkeypatch):
    client, result, _ = _account()
    original = InMemoryStore.credit_workspace_once

    def commit_then_disconnect(self, workspace_id, amount_microdollars, event_id):
        original(self, workspace_id, amount_microdollars, event_id)
        raise TimeoutError("lost response after commit")

    with monkeypatch.context() as patcher:
        patcher.setattr(InMemoryStore, "credit_workspace_once", commit_then_disconnect)
        assert _post(client, action="github", attested=True).status_code == 500
    assert client.get("/console/social-rewards").json()["actions"][0]["claimed"] is True
    assert _post(client, action="github", attested=True).status_code == 200
    assert live_credit_summary(result.workspace.id)["total_credits"] == 180_000


@pytest.mark.parametrize("body", [
    {"action": "github", "attested": False},
    {"action": "github", "attested": 1},
    {"action": "github", "attested": "true"},
    {"action": "github", "attested": True, "amount": 1_000_000},
    {"action": "github", "attested": True, "workspace_id": "other"},
    {"action": "unknown", "attested": True},
])
def test_forged_claims_rejected(body):
    client, result, _ = _account()
    assert _post(client, **body).status_code == 422
    assert live_credit_summary(result.workspace.id)["total_credits"] == 150_000


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.invalid", "https://testserver", "http://testserver:99"])
def test_claims_are_csrf_protected(origin):
    client, _, _ = _account()
    response = client.post("/console/social-rewards/claim", json={"action": "github", "attested": True},
                           headers={"Origin": origin} if origin else {})
    assert response.status_code == 403


def test_legacy_and_other_account_cannot_claim():
    client, _, _ = _account(enrolled=False)
    assert client.get("/console/social-rewards").json()["eligible"] is False
    assert _post(client, action="github", attested=True).status_code == 403


def test_disabled_and_peer_plane_cannot_claim():
    for overrides in [{"social_rewards_enabled": False}, {"social_rewards_plane_id": "aws"}]:
        client, result, _ = _account(_settings(**overrides))
        assert _post(client, action="github", attested=True).status_code == 409
        assert live_credit_summary(result.workspace.id)["total_credits"] == 150_000
        STORE.reset()


def test_shown_is_once_across_sessions_and_dismissal_keeps_claims_available():
    client, _, _ = _account()
    assert _post(client, "shown").json()["show_now"] is True
    assert _post(client, "shown").json()["show_now"] is False
    assert _post(client, "dismiss").json()["dismissed"] is True
    assert _post(client, action="github", attested=True).status_code == 200


def test_campaign_configuration_fails_closed_without_confirmed_destination():
    assert not campaign_enabled(Settings())
    assert not campaign_enabled(_settings(social_rewards_youtube_url=""))


def test_signup_enrollment_preserves_existing_exclusions():
    settings = _settings()
    for provider, email, created_at, expected in [
        ("google", "new@gmail.com", None, 150_000),
        ("github", "oldgithub@gmail.com", "2020-01-01T00:00:00Z", 150_000),
        ("github", "newgithub@gmail.com", "2999-01-01T00:00:00Z", 0),
        ("google", "excluded@hotmail.com", None, 0),
    ]:
        login = _persist_login(OAUTH_PROVIDERS[provider], settings, OAuthUserInfo(
            sub=email, email=email, email_verified=True, provider_account_created_at=created_at,
        ), None)
        assert login.attribution is not None
        assert login.attribution[1] == expected
        assert (STORE.get_social_reward_enrollment(login.user_id) is not None) == (expected > 0)
    returning = _persist_login(OAUTH_PROVIDERS["google"], settings, OAuthUserInfo(
        sub="new@gmail.com", email="new@gmail.com", email_verified=True,
    ), None)
    assert not returning.first_time


def test_first_call_eligibility_does_not_require_marketing_attribution(monkeypatch):
    from trusted_router.acquisition import record_successful_api_call_safely

    client, result, settings = _account(activated=False)
    # Equivalent to a GPC/DNT signup, where no marketing record is created.
    monkeypatch.setattr(InMemoryStore, "get_acquisition_attribution", lambda self, workspace_id: None)
    record_successful_api_call_safely(result.workspace.id, model="test", provider="test", settings=settings)
    assert _post(client, action="github", attested=True).status_code == 200
    assert STORE.get_social_reward_enrollment(result.user.id).first_call_complete


def test_failed_first_call_marker_does_not_fail_inference(monkeypatch):
    from trusted_router.acquisition import record_successful_api_call_safely

    _, result, settings = _account(activated=False)

    def unavailable(self, user_id):
        raise TimeoutError("storage unavailable")

    monkeypatch.setattr(InMemoryStore, "mark_social_reward_first_call", unavailable)
    record_successful_api_call_safely(result.workspace.id, model="test", provider="test", settings=settings)


def test_delegated_and_disabled_signup_keep_existing_amounts():
    for email, next_target, enabled, expected in [
        ("delegated@gmail.com", "/auth?callback_url=https%3A%2F%2Fexample.com%2Fcallback", True, 0),
        ("disabled@gmail.com", None, False, 300_000),
    ]:
        login = _persist_login(OAUTH_PROVIDERS["google"], _settings(social_rewards_enabled=enabled),
                               OAuthUserInfo(sub=email, email=email, email_verified=True), next_target)
        assert login.attribution[1] == expected
        assert STORE.get_social_reward_enrollment(login.user_id) is None


def test_wrong_account_has_no_access_to_enrollment():
    client, result, _ = _account()
    other = STORE.ensure_user("other@gmail.com")
    token, _ = STORE.create_auth_session(user_id=other.id, provider="google", label="other", ttl_seconds=3600)
    client.cookies.set("tr_session", token)
    assert client.get("/console/social-rewards").json()["eligible"] is False
    assert _post(client, action="github", attested=True).status_code == 403
    assert live_credit_summary(result.workspace.id)["total_credits"] == 150_000


def test_existing_authenticated_rate_limit_applies_to_claims():
    client, _, _ = _account(_settings(rate_limit_enabled=True, rate_limit_key_per_window=1))
    assert _post(client, action="github", attested=True).status_code == 200
    assert _post(client, action="github", attested=True).status_code == 429


def test_optional_analytics_failure_cannot_fail_a_claim(monkeypatch):
    from trusted_router import acquisition

    client, result, _ = _account()

    def failing_log(*args, **kwargs):
        raise RuntimeError("analytics unavailable")

    monkeypatch.setattr(acquisition, "_log_conversion", failing_log)
    assert _post(client, action="github", attested=True).status_code == 200
    assert live_credit_summary(result.workspace.id)["total_credits"] == 180_000


def test_click_is_not_a_claim():
    client, result, _ = _account()
    assert _post(client, "clicked", action="github").status_code == 200
    assert live_credit_summary(result.workspace.id)["total_credits"] == 150_000


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://attacker.invalid/@test", "http://youtube.com/@test"])
def test_invalid_destination_cannot_enable_campaign(url):
    assert not campaign_enabled(_settings(social_rewards_youtube_url=url))


@pytest.mark.parametrize("change", ["transfer", "delete"])
def test_original_destination_must_still_be_owned_and_active(change):
    client, result, _ = _account()
    if change == "transfer":
        other = STORE.ensure_user("newowner@gmail.com")
        STORE.transfer_workspace_ownership(result.workspace.id, other.id)
    else:
        STORE.update_workspace(result.workspace.id, deleted=True)
    # Keep an accessible workspace so console authentication can resolve.
    STORE.create_workspace(result.user.id, "Remaining")
    assert _post(client, action="github", attested=True).status_code == 403
