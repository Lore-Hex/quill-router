"""The same reservation arithmetic on every money backend."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from trusted_router.spend_windows import KeyWindowLimitExceeded
from trusted_router.strict_budget import StrictBudgetBusy


def test_strict_window_holds_refund_and_byok(store, workspace_id):
    _, key = store.create_api_key(
        workspace_id=workspace_id,
        creator_user_id=None,
        name="strict",
        budget_strict=True,
        limit_daily_microdollars=100,
        include_byok_in_limit=False,
    )
    assert store.get_key_by_hash(key.hash).budget_strict
    if hasattr(store, "authorize_gateway_typed"):
        # GCP's retired JSON reservation API must fail closed. Its production
        # typed path is covered in test_strict_budget_typed.py.
        with pytest.raises(ValueError, match="typed gateway authorization"):
            store.reserve_key_limit(key.hash, 60, usage_type="Credits")
        return
    first = store.reserve_key_limit(key.hash, 60, usage_type="Credits")
    assert first.reserved_microdollars == 60
    assert store.reserve_key_limit(key.hash, 1000, usage_type="BYOK").reserved_microdollars == 0
    with pytest.raises(KeyWindowLimitExceeded):
        store.reserve_key_limit(key.hash, 60, usage_type="Credits")
    store.refund_key_limit(key.hash, first.reserved_microdollars, usage_type="Credits")
    assert store.reserve_key_limit(key.hash, 100, usage_type="Credits").reserved_microdollars == 100


def test_default_window_mode_preserves_approximate_admission(store, workspace_id):
    _, key = store.create_api_key(
        workspace_id=workspace_id, creator_user_id=None, name="fast", limit_daily_microdollars=100
    )
    assert not key.budget_strict
    assert store.reserve_key_limit(key.hash, 60, usage_type="Credits").reserved_microdollars == 0
    assert store.reserve_key_limit(key.hash, 60, usage_type="Credits").reserved_microdollars == 0


def test_strict_concurrent_admission_never_over_reserves(store, workspace_id):
    _, key = store.create_api_key(
        workspace_id=workspace_id,
        creator_user_id=None,
        name="strict",
        budget_strict=True,
        limit_daily_microdollars=100,
    )
    if hasattr(store, "authorize_gateway_typed"):
        with pytest.raises(ValueError, match="typed gateway authorization"):
            store.reserve_key_limit(key.hash, 60, usage_type="Credits")
        return

    def reserve(_):
        try:
            return store.reserve_key_limit(key.hash, 60, usage_type="Credits").reserved_microdollars
        except KeyWindowLimitExceeded:
            return 0
        except StrictBudgetBusy:
            return 0

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(reserve, range(2))) == 60
