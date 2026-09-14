"""Funding must use the same USD book as inference, on every backend."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from trusted_router.security import new_api_key
from trusted_router.services.lightning import LightningCredits
from trusted_router.store_protocol import Store


def test_key_only_identity_is_atomic_and_has_no_grant(store: Store) -> None:
    raw = new_api_key()
    bridge = LightningCredits(store)
    workspace_id = bridge.resolve(raw, new=True)
    assert bridge.resolve(raw, new=True) == workspace_id
    assert bridge.resolve(raw, new=False) == workspace_id
    key = store.get_key_by_raw(raw)
    assert key and not key.management
    assert key.secret_hash != raw and key.lookup_hash != raw
    assert not key.scopes and not key.app_id
    workspace = store.get_workspace(workspace_id)
    assert workspace
    user = store.get_user(workspace.owner_user_id)
    assert user and user.email is None and user.wallet_address is None
    assert user.email_verified is False
    assert user.owner_workspace_count == 1
    assert bridge.balance(workspace_id) == 0


def test_concurrent_key_creation_keeps_one_identity(store: Store) -> None:
    bridge = LightningCredits(store)
    raw = new_api_key()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: bridge.resolve(raw, new=True), range(8)))
    assert len(set(results)) == 1
    assert bridge.balance(results[0]) == 0


@pytest.mark.parametrize("action", ["disabled", "deleted"])
def test_revoked_key_cannot_be_recreated(store: Store, action: str) -> None:
    bridge = LightningCredits(store)
    raw = new_api_key()
    bridge.resolve(raw, new=True)
    key = store.get_key_by_raw(raw)
    assert key
    if action == "deleted":
        store.delete_key(key.hash)
    else:
        store.update_key(key.hash, {"disabled": True})
    for new in (False, True):
        with pytest.raises(ValueError):
            bridge.resolve(raw, new=new)


def test_deposit_replay_is_exactly_once_even_after_lost_ack(store: Store) -> None:
    bridge = LightningCredits(store)
    workspace_id = bridge.resolve(new_api_key(), new=True)
    payment = uuid4().hex + uuid4().hex
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: bridge.credit(workspace_id, payment, 1_234_567), range(8)))
    assert bridge.balance(workspace_id) == 1_234_567
    bridge.credit(workspace_id, payment, 1_234_567)
    assert bridge.balance(workspace_id) == 1_234_567


def test_payment_cannot_change_amount_or_workspace(store: Store) -> None:
    bridge = LightningCredits(store)
    first = bridge.resolve(new_api_key(), new=True)
    other = bridge.resolve(new_api_key(), new=True)
    payment = uuid4().hex + uuid4().hex
    bridge.credit(first, payment, 1)
    for workspace_id, amount in ((first, 2), (other, 1)):
        with pytest.raises(ValueError, match="conflict"):
            bridge.credit(workspace_id, payment, amount)
    assert bridge.balance(first) == 1
    assert bridge.balance(other) == 0


def test_claim_without_credit_is_recoverable(store: Store) -> None:
    bridge = LightningCredits(store)
    workspace_id = bridge.resolve(new_api_key(), new=True)
    payment = uuid4().hex + uuid4().hex
    store.bind_lightning_payment(workspace_id, payment, 10_000_000)
    assert bridge.balance(workspace_id) == 0
    bridge.credit(workspace_id, payment, 10_000_000)
    assert bridge.balance(workspace_id) == 10_000_000


@pytest.mark.parametrize("amount", [True, 1.25, 0, -1, 1_000_000_001])
def test_payment_validation_precedes_every_write(store: Store, amount: int) -> None:
    bridge = LightningCredits(store)
    workspace_id = bridge.resolve(new_api_key(), new=True)
    with pytest.raises(ValueError):
        bridge.credit(workspace_id, uuid4().hex + uuid4().hex, amount)
    assert bridge.balance(workspace_id) == 0


def test_existing_inference_key_funds_original_workspace(store: Store, workspace_id: str, user_id: str) -> None:
    workspace = store.get_workspace(workspace_id)
    assert workspace
    raw, _key = store.create_api_key(workspace_id=workspace.id, name="existing", creator_user_id=user_id)
    bridge = LightningCredits(store)
    assert bridge.resolve(raw, new=False) == workspace.id
    assert bridge.resolve(raw, new=True) == workspace.id
    bridge.credit(workspace.id, uuid4().hex + uuid4().hex, 7_000_001)
    assert bridge.balance(workspace.id) == 7_000_001


def test_expired_existing_key_is_rejected(store: Store) -> None:
    bridge = LightningCredits(store)
    active = new_api_key()
    workspace_id = bridge.resolve(active, new=True)
    raw, _ = store.create_api_key(
        workspace_id=workspace_id, name="expired", creator_user_id=None,
        expires_at="2000-01-01T00:00:00Z",
    )
    for new in (False, True):
        with pytest.raises(ValueError):
            bridge.resolve(raw, new=new)
