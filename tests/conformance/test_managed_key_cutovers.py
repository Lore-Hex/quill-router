from trusted_router.store_protocol import Store


def test_promotion_is_idempotent_and_scoped_to_pair(store: Store, unique: str) -> None:
    assert not store.managed_key_promoted("fal", unique)
    for _ in range(2):
        store.record_managed_key_promotion("fal", unique)
        assert store.managed_key_promoted("fal", unique)
        assert not store.managed_key_promoted("minimax", unique)
        assert not store.managed_key_promoted("fal", unique + "-different")
