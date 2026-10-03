"""Workspace-scoped paging and revocation across the Store contract."""

from __future__ import annotations


def test_key_pages_and_bulk_delete(store, unique, user_id, workspace_id):
    # The shared fixtures create the user and workspace through calls every
    # backend implements (PostgresStore has no list_workspaces_for_user).
    other = store.create_workspace(user_id, f"other-{unique}", trial_credit_microdollars=0)
    raw_keys = [
        store.create_api_key(
            workspace_id=workspace_id,
            name=str(index),
            creator_user_id=user_id,
        )
        for index in range(5)
    ]
    raw_foreign, foreign = store.create_api_key(
        workspace_id=other.id,
        name="foreign",
        creator_user_id=user_id,
    )
    hashes = [key.hash for _, key in raw_keys]
    disabled = hashes[2]
    store.update_key(disabled, {"disabled": True})
    rows = store.list_api_keys_with_usage(workspace_id)
    expected = sorted((key for _, key in raw_keys), key=lambda key: key.hash)
    expected.sort(key=lambda key: key.created_at, reverse=True)
    assert [row.api_key.hash for row in rows] == [key.hash for key in expected]
    assert len(rows) == 5
    assert all(row.usage_microdollars == 0 for row in rows)
    pages = [
        store.list_api_keys_with_usage(workspace_id, limit=2, offset=offset) for offset in (0, 2, 4)
    ]
    assert [len(page) for page in pages] == [2, 2, 1]
    assert [row.api_key.hash for page in pages for row in page] == [key.hash for key in expected]
    filtered = store.list_api_keys_with_usage(workspace_id, include_disabled=False)
    assert [row.api_key.hash for row in filtered] == [
        key.hash for key in expected if key.hash != disabled
    ]
    assert store.list_api_keys_with_usage(workspace_id, offset=10**30) == []
    assert store.list_api_keys_with_usage(workspace_id, offset=5, limit=1) == []
    assert (
        store.list_api_keys_with_usage(workspace_id, offset=1, limit=1, include_disabled=False)
        == filtered[1:2]
    )
    assert store.delete_keys(workspace_id, [hashes[0], "missing", foreign.hash, hashes[0]]) == {
        hashes[0]: True,
        "missing": False,
        foreign.hash: False,
    }
    assert store.get_key_by_hash(hashes[0]) is None
    assert store.get_key_by_raw(raw_keys[0][0]) is None
    assert store.get_key_by_raw(raw_foreign) is not None
    assert len(store.list_api_keys_with_usage(workspace_id)) == 4
    assert store.delete_key(hashes[1]) is True
    assert store.delete_key(hashes[1]) is False
    assert store.get_key_by_raw(raw_keys[1][0]) is None
    assert len(store.list_api_keys_with_usage(workspace_id)) == 3
    assert store.delete_keys(workspace_id, []) == {}
