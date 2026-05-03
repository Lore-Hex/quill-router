from __future__ import annotations

import datetime as dt

from trusted_router.storage import Generation, InMemoryStore


def test_delete_key_removes_raw_lookup_path() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="delete me",
        creator_user_id=user.id,
    )

    assert store.get_key_by_raw(raw) is key
    assert store.delete_key(key.hash) is True
    assert store.get_key_by_hash(key.hash) is None
    assert store.get_key_by_raw(raw) is None
    assert store.list_keys(workspace.id) == []
    assert store.delete_key(key.hash) is False


def test_set_user_email_resets_verified_when_address_changes() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    verified = store.mark_user_email_verified(user.id)
    assert verified is not None and verified.email_verified is True

    updated = store.set_user_email(user.id, "new@example.com")

    assert updated is not None
    assert updated.email == "new@example.com"
    assert updated.email_verified is False
    assert store.find_user_by_email("alice@example.com") is None
    assert store.find_user_by_email("new@example.com").id == user.id


def test_update_workspace_persists_name_and_deleted_state() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    renamed = store.update_workspace(workspace.id, name="Renamed")
    deleted = store.update_workspace(workspace.id, deleted=True)

    assert renamed is not None
    assert renamed.name == "Renamed"
    assert deleted is not None
    assert deleted.deleted is True
    assert store.get_workspace(workspace.id) is None
    assert store.list_workspaces_for_user(user.id) == []


def test_key_limit_release_is_bounded_at_zero() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="limited",
        creator_user_id=user.id,
        limit_microdollars=1_000,
    )

    store.reserve_key_limit(key.hash, 400, usage_type="Credits")
    assert key.reserved_microdollars == 400
    store.refund_key_limit(key.hash, 900, usage_type="Credits")
    assert key.reserved_microdollars == 0
    store.settle_key_limit(key.hash, 900, 100, usage_type="Credits")
    assert key.reserved_microdollars == 0


def test_byok_key_limit_exclusion_skips_reservation_and_release() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="byok excluded",
        creator_user_id=user.id,
        limit_microdollars=1,
        include_byok_in_limit=False,
    )

    store.reserve_key_limit(key.hash, 999_999, usage_type="BYOK")
    assert key.reserved_microdollars == 0
    store.refund_key_limit(key.hash, 999_999, usage_type="BYOK")
    assert key.reserved_microdollars == 0


def test_activity_grouping_splits_credit_and_byok_usage() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        name="activity",
        creator_user_id=user.id,
    )

    store.add_generation(_generation("gen_credit", workspace.id, key.hash, "Credits", 300))
    store.add_generation(_generation("gen_byok", workspace.id, key.hash, "BYOK", 700))

    rows = store.activity(workspace.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["requests"] == 2
    assert row["usage_microdollars"] == 300
    assert row["byok_usage_inference_microdollars"] == 700
    assert row["prompt_tokens"] == 20
    assert row["completion_tokens"] == 10
    assert key.usage_microdollars == 300
    assert key.byok_usage_microdollars == 700


def test_activity_events_filter_by_key_date_and_limit() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _raw1, key1 = store.create_api_key(
        workspace_id=workspace.id,
        name="key 1",
        creator_user_id=user.id,
    )
    _raw2, key2 = store.create_api_key(
        workspace_id=workspace.id,
        name="key 2",
        creator_user_id=user.id,
    )
    store.add_generation(_generation("gen_1", workspace.id, key1.hash, "Credits", 100, "2026-05-01T00:00:00Z"))
    store.add_generation(_generation("gen_2", workspace.id, key1.hash, "Credits", 200, "2026-05-02T00:00:00Z"))
    store.add_generation(_generation("gen_3", workspace.id, key2.hash, "Credits", 300, "2026-05-02T01:00:00Z"))

    rows = store.activity_events(workspace.id, api_key_hash=key1.hash, date="2026-05-02", limit=1)

    assert [row["id"] for row in rows] == ["gen_2"]
    assert rows[0]["cost_microdollars"] == 200
    assert rows[0]["content_stored"] is False


def test_rate_limit_buckets_reset_and_stale_entries_are_cleaned() -> None:
    store = InMemoryStore()
    start = dt.datetime(2026, 5, 2, tzinfo=dt.UTC)

    first = store.hit_rate_limit(
        namespace="ip",
        subject="1.2.3.4",
        limit=2,
        window_seconds=60,
        now=start,
    )
    second = store.hit_rate_limit(
        namespace="ip",
        subject="1.2.3.4",
        limit=2,
        window_seconds=60,
        now=start,
    )
    third = store.hit_rate_limit(
        namespace="ip",
        subject="1.2.3.4",
        limit=2,
        window_seconds=60,
        now=start,
    )
    later = store.hit_rate_limit(
        namespace="ip",
        subject="1.2.3.4",
        limit=2,
        window_seconds=60,
        now=start + dt.timedelta(minutes=4),
    )

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.remaining == 0
    assert later.allowed is True
    assert len(store.rate_limit_store.buckets) == 1


def _generation(
    generation_id: str,
    workspace_id: str,
    key_hash: str,
    usage_type: str,
    cost_microdollars: int,
    created_at: str = "2026-05-02T00:00:00Z",
) -> Generation:
    return Generation(
        id=generation_id,
        request_id=f"req_{generation_id}",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model="openai/gpt-4o-mini",
        provider_name="OpenAI",
        app="test",
        tokens_prompt=10,
        tokens_completion=5,
        total_cost_microdollars=cost_microdollars,
        usage_type=usage_type,
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        created_at=created_at,
    )
