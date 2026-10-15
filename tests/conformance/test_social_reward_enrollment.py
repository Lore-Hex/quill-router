"""Campaign enrollment is atomic with signup money, never inferred from balances."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from trusted_router.store_protocol import Store
from trusted_router.typed_balance import live_credit_summary


def test_social_enrollment_keeps_initial_destination_and_cohort(store: Store, unique: str) -> None:
    user = store.ensure_user(
        f"social-{unique}@example.com",
        trial_credit_microdollars=150_000,
        social_rewards_issuer="primary",
    )
    enrollment = store.get_social_reward_enrollment(user.id)
    assert enrollment is not None
    assert enrollment.issuer == "primary"
    assert enrollment.dismissed is False
    summary = live_credit_summary(enrollment.workspace_id, store=store)
    assert summary is not None and summary["total_credits"] == 150_000
    other = store.create_workspace(owner_user_id=user.id, name="Other")
    assert other.id != enrollment.workspace_id
    assert store.ensure_user(
        f"social-{unique}@example.com", trial_credit_microdollars=150_000, social_rewards_issuer="peer"
    ).id == user.id
    assert store.get_social_reward_enrollment(user.id) == enrollment
    legacy = store.ensure_user(f"legacy-{unique}@example.com", trial_credit_microdollars=300_000)
    store.ensure_user(
        f"legacy-{unique}@example.com", trial_credit_microdollars=150_000, social_rewards_issuer="primary"
    )
    assert store.get_social_reward_enrollment(legacy.id) is None


@pytest.mark.parametrize("amount", [None, 0, 30_000, 300_000])
def test_social_enrollment_rejects_wrong_signup_amount(
    store: Store, unique: str, amount: int | None,
) -> None:
    email = f"invalid-{unique}@example.com"
    with pytest.raises(ValueError, match="150000"):
        store.ensure_user(email, trial_credit_microdollars=amount, social_rewards_issuer="primary")
    assert store.find_user_by_email(email) is None


def test_social_dismissal_is_saved_and_once(store: Store, unique: str) -> None:
    user = store.ensure_user(
        f"dismiss-{unique}@example.com",
        trial_credit_microdollars=150_000,
        social_rewards_issuer="primary",
    )
    assert store.dismiss_social_rewards(user.id) is True
    assert store.dismiss_social_rewards(user.id) is False
    enrollment = store.get_social_reward_enrollment(user.id)
    assert enrollment is not None and enrollment.dismissed
    assert store.dismiss_social_rewards(f"missing-{unique}") is False


def test_social_committed_grants_reconcile_after_response_loss(store: Store, unique: str) -> None:
    user = store.ensure_user(
        f"grants-{unique}@example.com",
        trial_credit_microdollars=150_000,
        social_rewards_issuer="primary",
    )
    enrollment = store.get_social_reward_enrollment(user.id)
    assert enrollment is not None
    workspace_id = enrollment.workspace_id
    for action in range(5):
        event = f"social-rewards:{user.id}:{action}"
        assert not store.has_credit_grant(workspace_id, event)
        assert store.credit_workspace_once(workspace_id, 30_000, event)
        # Lost response/restarted request reads committed provenance and retries same key.
        assert store.has_credit_grant(workspace_id, event)
        assert not store.has_credit_grant(f"other-{unique}", event)
        assert not store.credit_workspace_once(workspace_id, 30_000, event)
    summary = live_credit_summary(workspace_id, store=store)
    assert summary is not None and summary["total_credits"] == 300_000


def test_social_concurrent_claim_is_exactly_once(store: Store, unique: str) -> None:
    user = store.ensure_user(
        f"concurrent-{unique}@example.com",
        trial_credit_microdollars=150_000,
        social_rewards_issuer="primary",
    )
    enrollment = store.get_social_reward_enrollment(user.id)
    assert enrollment is not None
    event = f"social-rewards:{user.id}:github"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: store.credit_workspace_once(enrollment.workspace_id, 30_000, event),
            range(4),
        ))
    assert results.count(True) == 1
    assert store.has_credit_grant(enrollment.workspace_id, event)
    summary = live_credit_summary(enrollment.workspace_id, store=store)
    assert summary is not None and summary["total_credits"] == 180_000


def test_social_first_call_is_durable_and_idempotent(store: Store, unique: str) -> None:
    user = store.ensure_user(
        f"first-call-{unique}@example.com",
        trial_credit_microdollars=150_000,
        social_rewards_issuer="primary",
    )
    enrollment = store.get_social_reward_enrollment(user.id)
    assert enrollment is not None and not enrollment.first_call_complete
    assert store.mark_social_reward_first_call(user.id)
    assert not store.mark_social_reward_first_call(user.id)
    enrollment = store.get_social_reward_enrollment(user.id)
    assert enrollment is not None and enrollment.first_call_complete
    assert not enrollment.dismissed
    assert not store.mark_social_reward_first_call(f"missing-{unique}")


def test_spending_signup_credit_does_not_reset_campaign(store: Store, unique: str) -> None:
    user = store.ensure_user(
        f"spent-{unique}@example.com", trial_credit_microdollars=150_000,
        social_rewards_issuer="primary",
    )
    enrollment = store.get_social_reward_enrollment(user.id)
    assert enrollment is not None
    _, key = store.create_api_key(workspace_id=enrollment.workspace_id, name="spend", creator_user_id=user.id)
    reservation = store.reserve(enrollment.workspace_id, key.hash, 150_000)
    store.settle(reservation.id, 150_000)
    for action in range(5):
        event = f"social-rewards:{user.id}:{action}"
        assert store.credit_workspace_once(enrollment.workspace_id, 30_000, event)
        assert not store.credit_workspace_once(enrollment.workspace_id, 30_000, event)
    summary = live_credit_summary(enrollment.workspace_id, store=store)
    assert summary is not None
    assert summary["total_credits"] == 300_000
    assert summary["total_usage"] == 150_000
    assert summary["available"] == 150_000
