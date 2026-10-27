"""Access closure preserves money and permanently gates identity on each store."""
import copy

import pytest

from trusted_router.storage_account_closure import AccountClosureBlocked
from trusted_router.typed_balance import live_credit_summary


def test_account_closure_is_atomic_idempotent_and_preserves_credits(store, user_id, workspace_id):
    raw, key = store.create_api_key(workspace_id=workspace_id, name="close", creator_user_id=user_id)
    session, _ = store.create_auth_session(user_id=user_id, provider="github", label="close", ttl_seconds=3600)
    before = live_credit_summary(workspace_id, store=store)
    account_before = copy.deepcopy(store.get_credit_account(workspace_id))
    result = store.close_user_account(user_id)
    assert result.disabled and result.account_closed_at and result.owner_workspace_count == 0
    assert store.close_user_account(user_id).account_closed_at == result.account_closed_at
    assert store.get_workspace(workspace_id) is None
    assert store.get_key_by_hash(key.hash).disabled
    assert store.api_key_auth_context(raw).workspace is None
    assert store.session_auth_context(session) is None
    assert store.get_auth_session_by_raw(session) is None
    assert live_credit_summary(workspace_id, store=store) == before
    after = store.get_credit_account(workspace_id)
    assert after is not None and not after.auto_refill_enabled
    assert after.stripe_customer_id == account_before.stripe_customer_id
    assert store.find_user_by_email(result.email).id == user_id
    with pytest.raises(ValueError, match="account_closed"):
        store.create_workspace(user_id, "Cannot reopen")


def test_shared_account_closure_rolls_back(store, user_id, workspace_id, unique):
    store.add_members(workspace_id, [f"other-{unique}@example.com"])
    with pytest.raises(AccountClosureBlocked, match="shared"):
        store.close_user_account(user_id)
    assert not store.get_user(user_id).disabled
    assert store.get_workspace(workspace_id)


def test_account_closure_missing_user_is_noop(store, unique):
    assert store.close_user_account(f"absent-{unique}") is None


def test_account_closure_covers_all_owned_workspaces(store, user_id, workspace_id):
    extra = store.create_workspace(user_id, "Second personal workspace")
    _, key = store.create_api_key(workspace_id=extra.id, name="second", creator_user_id=user_id)
    before = live_credit_summary(extra.id, store=store)
    store.close_user_account(user_id)
    assert store.get_workspace(workspace_id) is None
    assert store.get_workspace(extra.id) is None
    assert store.get_key_by_hash(key.hash).disabled
    assert store.get_user(user_id).owner_workspace_count == 0
    assert live_credit_summary(extra.id, store=store) == before


def test_membership_in_someone_elses_workspace_blocks_closure(store, user_id, workspace_id, unique):
    other = store.ensure_user(f"owner-{unique}@example.com")
    other_workspace = store.create_workspace(other.id, "Shared workspace")
    store.add_members(other_workspace.id, [store.get_user(user_id).email])
    with pytest.raises(AccountClosureBlocked, match="shared"):
        store.close_user_account(user_id)
    assert not store.get_user(user_id).disabled
    assert store.get_workspace(workspace_id)
    assert store.get_workspace(other_workspace.id)
