"""Workspace-scoped paging and revocation across the Store contract."""

from __future__ import annotations

import pytest

from trusted_router.storage_key_patch import KEY_LIMIT_FIELDS, StrictKeyFirstLimit


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
    # The contract is newest first with a deterministic tie-break. Keys created
    # in the same instant tie on created_at, and how the hash tie-break sorts
    # mixed case depends on the database collation (en_US on stock Postgres,
    # byte order on Spanner), so derive the order from the store itself.
    expected = [row.api_key for row in rows]
    assert sorted(key.hash for key in expected) == sorted(hashes)
    created = [key.created_at for key in expected]
    assert created == sorted(created, reverse=True)
    assert [row.api_key.hash for row in store.list_api_keys_with_usage(workspace_id)] == [
        key.hash for key in expected
    ]
    assert len(rows) == 5
    assert all(row.usage_microdollars == 0 for row in rows)
    pages = [
        store.list_api_keys_with_usage(workspace_id, limit=2, offset=offset) for offset in (0, 2, 4)
    ]
    assert [len(page) for page in pages] == [2, 2, 1]
    assert [row.api_key.hash for page in pages for row in page] == [key.hash for key in expected]
    assert store.list_api_keys_with_usage(workspace_id, offset=1) == rows[1:]
    assert store.list_api_keys_with_usage(workspace_id, offset=999999999999999999) == []
    assert store.list_api_keys_with_usage(workspace_id, offset=2**63 - 2, limit=1001) == []
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


def _hold(store, key_hash: str, amount: int) -> bool:
    """Take a key-limit hold the way the gateway does on this backend: the
    typed conditional DML on a SpannerStore (its reserve_key_limit is the
    retired JSON path), the store's reserve elsewhere."""
    from trusted_router.spend_windows import KeyLimitExceeded

    if hasattr(store, "_database") and hasattr(store, "_param_types"):
        from trusted_router.storage_gcp_counter_dml import KEY_ACCEPTED, reserve_key

        outcome = store._database.run_in_transaction(
            lambda t: reserve_key(t, store._param_types, key_hash, amount, is_byok=False)
        )
        return outcome == KEY_ACCEPTED
    try:
        return store.reserve_key_limit(key_hash, amount, usage_type="Credits").reserved_microdollars == amount
    except KeyLimitExceeded:
        return False


def test_chat_browser_key_index_and_freeze(store, unique, user_id, workspace_id):
    """The per-user chat key index reads only that user's keys, and freezing a
    key (disable plus a lifetime cap at what it used and holds) refuses any
    later hold, on every backend."""

    other_user = f"other-user-{unique}"
    _raw_a, key_a = store.create_api_key(
        workspace_id=workspace_id, name="chat-browser-a", creator_user_id=user_id,
        limit_microdollars=5_000_000,
    )
    _raw_b, key_b = store.create_api_key(
        workspace_id=workspace_id, name="chat-browser-b", creator_user_id=user_id,
        limit_microdollars=5_000_000,
    )
    _raw_c, key_c = store.create_api_key(
        workspace_id=workspace_id, name="chat-browser-c", creator_user_id=other_user,
        limit_microdollars=5_000_000,
    )
    store.record_chat_browser_key(workspace_id, user_id, key_a.hash)
    store.record_chat_browser_key(workspace_id, user_id, key_b.hash)
    store.record_chat_browser_key(workspace_id, other_user, key_c.hash)
    # An index row naming someone else's key is ignored when listing.
    store.record_chat_browser_key(workspace_id, user_id, key_c.hash)

    assert sorted(store.chat_browser_key_hashes(workspace_id, user_id)) == sorted(
        [key_a.hash, key_b.hash, key_c.hash]
    )
    assert store.chat_browser_key_hashes(workspace_id, other_user) == [key_c.hash]
    assert _hold(store, key_a.hash, 1_000_000)

    listed = {row.api_key.hash: row for row in store.list_chat_browser_keys(workspace_id, user_id)}
    assert sorted(listed) == sorted([key_a.hash, key_b.hash])
    assert listed[key_a.hash].reserved_microdollars == 1_000_000
    assert listed[key_b.hash].reserved_microdollars == 0
    only_b = store.list_chat_browser_keys(workspace_id, user_id, key_hashes=[key_b.hash])
    assert [row.api_key.hash for row in only_b] == [key_b.hash]

    assert _hold(store, key_b.hash, 1)  # control: an unfrozen key still takes holds
    frozen = store.update_key(key_a.hash, {"disabled": True, "limit_microdollars": 1_000_000})
    assert frozen is not None and frozen.disabled and frozen.limit_microdollars == 1_000_000
    assert not _hold(store, key_a.hash, 1), "a frozen key took a new hold"
    # The hold taken before the freeze is still counted.
    [after] = store.list_chat_browser_keys(workspace_id, user_id, key_hashes=[key_a.hash])
    assert after.reserved_microdollars == 1_000_000 and after.api_key.disabled

    store.forget_chat_browser_keys(workspace_id, user_id, [key_a.hash, key_c.hash])
    assert store.chat_browser_key_hashes(workspace_id, user_id) == [key_b.hash]
    assert store.chat_browser_key_hashes(workspace_id, other_user) == [key_c.hash]


def test_forget_expired_chat_browser_keys_prunes_only_expired_rows(store, unique, user_id, workspace_id):
    """Each backend stores a row's expiry with the index row itself, so the
    owner-side prune removes only rows that expired before the cutoff and keeps
    unexpired rows and rows with no recorded expiry."""

    other_user = f"other-user-{unique}"
    hashes = {}
    for name in ("past", "future", "unknown", "other-past"):
        _raw, key = store.create_api_key(
            workspace_id=workspace_id, name=f"chat-browser-{name}",
            creator_user_id=other_user if name == "other-past" else user_id,
            limit_microdollars=5_000_000,
        )
        hashes[name] = key.hash
    store.record_chat_browser_key(workspace_id, user_id, hashes["past"], expires_at="2026-01-01T00:00:00Z")
    store.record_chat_browser_key(workspace_id, user_id, hashes["future"], expires_at="2099-01-01T00:00:00Z")
    store.record_chat_browser_key(workspace_id, user_id, hashes["unknown"])
    store.record_chat_browser_key(workspace_id, other_user, hashes["other-past"], expires_at="2026-01-01T00:00:00Z")

    store.forget_expired_chat_browser_keys(workspace_id, user_id, expired_before="2026-06-01T00:00:00Z")

    assert sorted(store.chat_browser_key_hashes(workspace_id, user_id)) == sorted(
        [hashes["future"], hashes["unknown"]]
    )
    # Another user's rows are never touched by this user's prune.
    assert store.chat_browser_key_hashes(workspace_id, other_user) == [hashes["other-past"]]


def test_limit_at_most_lowers_and_never_raises(store, unique, user_id, workspace_id):
    """``limit_microdollars_at_most`` is a conditional lower applied to the key
    read inside each backend's write transaction: it cuts a higher limit,
    leaves a lower one (and the disabled flag) alone, and the typed limit row
    follows, so a hold above the result is refused."""
    _raw, key = store.create_api_key(
        workspace_id=workspace_id, name="chat-browser-lower", creator_user_id=user_id,
        limit_microdollars=5_000_000,
    )
    lowered = store.update_key(key.hash, {"limit_microdollars_at_most": 2_000_000})
    assert lowered is not None and lowered.limit_microdollars == 2_000_000
    frozen = store.update_key(key.hash, {"disabled": True, "limit_microdollars_at_most": 1_000_000})
    assert frozen is not None and frozen.disabled and frozen.limit_microdollars == 1_000_000
    # A later, higher lower (say from an older read) changes nothing.
    kept = store.update_key(key.hash, {"limit_microdollars_at_most": 4_000_000})
    assert kept is not None and kept.limit_microdollars == 1_000_000 and kept.disabled
    assert not _hold(store, key.hash, 1_000_001)
    assert _hold(store, key.hash, 1_000_000)  # the typed row allows exactly the limit


def test_a_limit_edit_keeps_the_keys_holds(store, unique, user_id, workspace_id):
    """A typed limit write must not touch the counters: a hold taken before a
    budget edit is still counted after it, so the new limit is enforced
    against it. (On the Spanner emulator a partial tr_key_limit upsert reset
    the omitted counter columns to their defaults.)"""
    _raw, key = store.create_api_key(
        workspace_id=workspace_id, name="edited", creator_user_id=user_id,
        limit_microdollars=5_000_000,
    )
    assert _hold(store, key.hash, 1_000_000)
    assert store.update_key(key.hash, {"limit_microdollars": 4_000_000}) is not None
    assert _hold(store, key.hash, 3_000_000)
    assert not _hold(store, key.hash, 1), "the edit forgot the $1 hold"


def _limits(store, key_hash: str) -> tuple[int | None, ...]:
    """The key's four limits as committed: the entity's, and on a backend with
    a typed counter row its tr_key_limit shard 0 mirror too, which must say
    the same (a single-shard key's lifetime limit is the whole limit)."""
    stored = store.get_key_by_hash(key_hash)
    assert stored is not None
    entity = tuple(getattr(stored, field) for field in KEY_LIMIT_FIELDS)
    mirror = _mirror(store, key_hash)
    if mirror is not None:
        assert mirror == entity, f"the tr_key_limit mirror {mirror} is not the entity's {entity}"
    return entity


_MIRROR_SQL = ("SELECT limit_micro, day_limit_micro, week_limit_micro, month_limit_micro FROM tr_key_limit "
               "WHERE key_hash = {} AND shard = 0")


def _mirror(store, key_hash: str) -> tuple[int | None, ...] | None:
    """tr_key_limit shard 0's mirrored limits as committed, read the backend's
    own way; None on a backend with no typed counter rows (memory)."""
    if hasattr(store, "_database") and hasattr(store, "_param_types"):
        typed = getattr(store._database, "typed", None)
        if typed is not None:
            row = typed.get("tr_key_limit", {}).get((key_hash, 0))
            assert row is not None, "no tr_key_limit row"
            return tuple(row.get(c) for c in ("limit_micro", "day_limit_micro", "week_limit_micro",
                                              "month_limit_micro"))
        with store._database.snapshot() as snapshot:
            rows = list(snapshot.execute_sql(_MIRROR_SQL.format("@kh"), params={"kh": key_hash},
                                             param_types={"kh": store._param_types.STRING}))
        assert len(rows) == 1, "no tr_key_limit row"
        return tuple(None if v is None else int(v) for v in rows[0])
    pool = getattr(store, "_pool", None)
    if pool is None:
        return None
    with pool.connection() as conn:
        row = conn.execute(_MIRROR_SQL.format("%s"), (key_hash,), prepare=False).fetchone()
    assert row is not None, "no tr_key_limit row"
    return tuple(None if v is None else int(v) for v in row)


def test_a_limitless_strict_key_never_gains_a_limit(store, unique, user_id, workspace_id):
    """Every backend refuses, inside its write transaction or under its lock,
    a patch that would give a limitless budget_strict key its first limit,
    and writes nothing: neither the entity nor its typed limit row. Limits a
    strict key already has can change and be removed, and once removed the
    key stays limitless (storage_key_patch.StrictKeyFirstLimit)."""
    _raw, key = store.create_api_key(
        workspace_id=workspace_id, name="strict-limitless", creator_user_id=user_id, budget_strict=True,
    )
    for patch in (
        {"limit_microdollars": 5_000_000},
        {"limit_microdollars_at_most": 5_000_000},
        {"limit_daily_microdollars": 1_000_000},
        {"limit_weekly_microdollars": 2_000_000},
        {"limit_monthly_microdollars": 3_000_000},
        {"name": "renamed", "limit_daily_microdollars": 1_000_000},
    ):
        with pytest.raises(StrictKeyFirstLimit):
            store.update_key(key.hash, patch)
        assert _limits(store, key.hash) == (None, None, None, None), patch
        assert store.get_key_by_hash(key.hash).name == "strict-limitless"
    renamed = store.update_key(key.hash, {"name": "renamed", "limit_daily_microdollars": None})
    assert renamed is not None and renamed.name == "renamed"
    assert _limits(store, key.hash) == (None, None, None, None)

    _raw, limited = store.create_api_key(
        workspace_id=workspace_id, name="strict-limited", creator_user_id=user_id, budget_strict=True,
        limit_daily_microdollars=5_000_000,
    )
    assert store.update_key(limited.hash, {"limit_daily_microdollars": 6_000_000}) is not None
    assert store.update_key(limited.hash, {"limit_monthly_microdollars": 9_000_000}) is not None
    assert _limits(store, limited.hash) == (None, 6_000_000, None, 9_000_000)
    assert store.update_key(limited.hash, {"limit_daily_microdollars": None}) is not None
    assert store.update_key(limited.hash, {"limit_monthly_microdollars": None}) is not None
    assert _limits(store, limited.hash) == (None, None, None, None)
    with pytest.raises(StrictKeyFirstLimit):
        store.update_key(limited.hash, {"limit_monthly_microdollars": 9_000_000})
    assert _limits(store, limited.hash) == (None, None, None, None)

    # A key that is not strict gains a first limit as before.
    _raw, plain = store.create_api_key(
        workspace_id=workspace_id, name="plain", creator_user_id=user_id,
    )
    assert store.update_key(plain.hash, {"limit_daily_microdollars": 1_000_000}) is not None
    assert _limits(store, plain.hash) == (None, 1_000_000, None, None)


def _force_entity(store, key) -> None:
    """Write an api_key entity raw, bypassing apply_key_patch and the mirror,
    the way an unguarded writer (a replay, a script) would."""
    if hasattr(store, "_database") and hasattr(store, "_param_types"):
        store._write_entity("api_key", key.hash, key)
    else:
        store._run_transaction(lambda conn: store._write_entity_tx(conn, "api_key", key.hash, key))


def test_a_strict_keys_mirror_never_gains_a_limit_whatever_writes_the_entity(
        store, unique, user_id, workspace_id):
    """The runtime chokepoint (storage_gcp_key_limit_mirror.write_key_limit_rows
    on Spanner, PostgresStore._write_key_limit_caps_tx on Postgres), in the
    writing transaction: a strict key whose tr_key_limit row has no limit
    never gains one, even when its entity was given one behind
    apply_key_patch's back; a key that is not strict still does."""
    _raw, key = store.create_api_key(
        workspace_id=workspace_id, name="strict-mirror", creator_user_id=user_id, budget_strict=True,
    )
    if _mirror(store, key.hash) is None:
        pytest.skip("no typed counter rows on this backend; apply_key_patch is the rule")
    forced = store.get_key_by_hash(key.hash)
    forced.limit_daily_microdollars = 5_000_000
    _force_entity(store, forced)
    with pytest.raises(StrictKeyFirstLimit):
        store.update_key(key.hash, {"include_byok_in_limit": False})
    assert _mirror(store, key.hash) == (None, None, None, None)
    assert store.get_key_by_hash(key.hash).include_byok_in_limit is True

    _raw, plain = store.create_api_key(workspace_id=workspace_id, name="plain-mirror", creator_user_id=user_id)
    forced = store.get_key_by_hash(plain.hash)
    forced.limit_daily_microdollars = 5_000_000
    _force_entity(store, forced)
    assert store.update_key(plain.hash, {"include_byok_in_limit": False}) is not None
    assert _mirror(store, plain.hash) == (None, 5_000_000, None, None)


def test_a_new_key_mirrors_its_creation_limits(store, unique, user_id, workspace_id):
    """Creation writes the mirror through the same chokepoint, as an insert
    of a new row (Spanner) or a row created with its limits (Postgres)."""
    _raw, key = store.create_api_key(
        workspace_id=workspace_id, name="strict-created", creator_user_id=user_id, budget_strict=True,
        limit_microdollars=7_000_000, limit_weekly_microdollars=3_000_000,
    )
    assert _limits(store, key.hash) == (7_000_000, None, 3_000_000, None)


def test_a_federated_strict_key_never_gains_a_limit_on_the_peer(store, unique, user_id, workspace_id):
    """A peer plane copies the home plane's key record; its first copy
    creates the row with the record's limits, and a later record that would
    give a limitless strict key a limit is refused there too."""
    record = {
        "lookup_hash": f"lh-strict-{unique}", "key_hash": f"kh-strict-{unique}",
        "workspace_id": f"ws-strict-{unique}", "name": "federated-strict", "disabled": False,
        "budget_strict": True, "include_byok_in_limit": True, "revision": "2026-11-23T00:00:00Z",
    }
    try:
        key = store.upsert_federated_api_key(record)
    except NotImplementedError:
        pytest.skip("backend is a federation HOME plane; it does not import keys")
    if _mirror(store, key.hash) is None:
        pytest.skip("no typed counter rows on this backend")
    assert _mirror(store, key.hash) == (None, None, None, None)
    with pytest.raises(StrictKeyFirstLimit):
        store.upsert_federated_api_key({**record, "limit_daily_microdollars": 1_000_000})
    assert _mirror(store, key.hash) == (None, None, None, None)

    limited = {**record, "key_hash": f"kh-strict2-{unique}", "lookup_hash": f"lh-strict2-{unique}",
               "limit_monthly_microdollars": 9_000_000}
    key = store.upsert_federated_api_key(limited)
    assert _limits(store, key.hash) == (None, None, None, 9_000_000)
    store.upsert_federated_api_key({**limited, "limit_monthly_microdollars": 8_000_000})
    assert _limits(store, key.hash) == (None, None, None, 8_000_000)
