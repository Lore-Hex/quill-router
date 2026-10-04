"""Durable batch claims: the same contract on every registered store backend."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from trusted_router.store_protocol import Store

NOW = "2026-09-01T12:00:00Z"


def test_retirement_claims_are_per_workspace_forever(store: Store, workspace_id: str, unique: str):
    assert store.claim_retirement_notices(workspace_id, [], occurred_at=NOW) == []
    assert store.claim_retirement_notices(workspace_id, ["b", "a", "a"], occurred_at=NOW) == ["a", "b"]
    assert store.claim_retirement_notices(workspace_id, ["a", "b"], occurred_at="2030-01-01T00:00:00Z") == []
    assert store.claim_retirement_notices(workspace_id, ["b", "c"], occurred_at=NOW) == ["c"]
    user = store.ensure_user(f"another-{unique}", f"another-{unique}@example.com")
    other = store.create_workspace(user.id, "Other", trial_credit_microdollars=0)
    assert store.claim_retirement_notices(other.id, ["a", "b"], occurred_at=NOW) == ["a", "b"]


def test_concurrent_retirement_claims_keep_the_batch_together(store: Store, workspace_id: str):
    barrier = Barrier(6)

    def claim(_):
        barrier.wait()
        return store.claim_retirement_notices(workspace_id, ["c", "a", "b"], occurred_at=NOW)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert results.count(["a", "b", "c"]) == 1
    assert results.count([]) == 5


def test_retirement_recipient_blocks_are_normalized_and_durable(store: Store, unique: str):
    address = f"notice-{unique}@example.com"
    assert store.is_email_blocked(address) is False
    assert store.get_email_block(address) is None
    block = store.block_email_sending(email=f" {address.upper()} ", reason="complaint")
    assert block.email == address
    assert store.is_email_blocked(address.upper()) is True
    assert store.get_email_block(address).reason == "complaint"


def test_notice_members_are_workspace_scoped(store: Store, workspace_id: str, user_id: str, unique: str):
    other = store.create_workspace(user_id, f"Other {unique}", trial_credit_microdollars=0)
    members = store.list_members(workspace_id)
    assert [(member.workspace_id, member.user_id, member.role) for member in members] == [
        (workspace_id, user_id, "owner"),
    ]
    assert [(member.workspace_id, member.role) for member in store.list_members(other.id)] == [
        (other.id, "owner"),
    ]
