from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    authorize_atomic,
    settle_atomic,
)
from trusted_router.storage_gcp_counter_reconcile import (
    backsync_typed_to_json,
    compare,
    repair_typed_reserved,
)
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    KEY_LIMIT_TABLE,
    key_usage_shard_count,
)
from trusted_router.storage_gcp_key_shard_admin import (
    inspect_key_usage_reshard,
    reshard_key_usage,
)
from trusted_router.storage_models import CreditAccount, Workspace


def _seed(*, key_shards: int = 4) -> tuple[Any, Any, Any]:
    store, database, _ = make_fake_store()
    workspace_id = "ws-key-shards"
    store._write_entity(
        "workspace",
        workspace_id,
        Workspace(
            id=workspace_id,
            name="Key shard test",
            owner_user_id="owner",
            billing_paused=True,
        ),
    )
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            total_credits_microdollars=1_000_000,
        ),
    )
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="uncapped-sharded-key",
        creator_user_id=None,
        limit_microdollars=None,
    )
    key.usage_shard_count = key_shards
    store._write_entity("api_key", key.hash, key)
    return store, database, key


def _auth_body(authorization_id: str, reservation_id: str) -> str:
    return json.dumps(
        {"id": authorization_id, "credit_reservation_id": reservation_id}
    )


def test_key_usage_shards_default_and_fail_closed_for_capped_keys() -> None:
    assert key_usage_shard_count({}) == 1
    assert key_usage_shard_count({"usage_shard_count": 16}) == 16
    with pytest.raises(ValueError, match="positive integer"):
        key_usage_shard_count({"usage_shard_count": 0})
    with pytest.raises(ValueError, match="must not exceed"):
        key_usage_shard_count({"usage_shard_count": 65})
    with pytest.raises(ValueError, match="only uncapped"):
        key_usage_shard_count(
            {"usage_shard_count": 2, "limit_microdollars": 1_000_000}
        )
    with pytest.raises(ValueError, match="only uncapped"):
        key_usage_shard_count(
            {"usage_shard_count": 2, "limit_daily_microdollars": 1_000_000}
        )


def test_api_key_mirror_creates_every_usage_shard_without_clobbering_counters() -> None:
    store, database, key = _seed(key_shards=4)
    rows = database.typed[KEY_LIMIT_TABLE]

    assert {(key.hash, shard) for shard in range(4)} <= set(rows)
    for shard in range(4):
        row = rows[(key.hash, shard)]
        assert row["limit_micro"] is None
        assert row["usage"] == 0
        assert row["byok_usage"] == 0
        assert row["reserved"] == 0

    rows[(key.hash, 2)]["usage"] = 123
    key.name = "renamed"
    store._write_entity("api_key", key.hash, key)
    assert rows[(key.hash, 2)]["usage"] == 123


def test_authorize_records_key_shard_and_settle_spreads_exact_usage() -> None:
    store, database, key = _seed(key_shards=4)
    reservations: list[str] = []

    for index in range(40):
        first = index % 4
        candidates = tuple((first + offset) % 4 for offset in range(4))
        result = authorize_atomic(
            store._database,
            store._param_types,
            workspace_id="ws-key-shards",
            key_hash=key.hash,
            estimate=1_000,
            has_credit_candidate=True,
            reservation_usage_type="Credits",
            idempotency_scope=f"key-shard-{index}",
            idempotency_fingerprint="same-body",
            expires_at="2026-12-01T00:00:00Z",
            build_auth_body=_auth_body,
            key_shard_candidates=candidates,
        )
        assert result["outcome"] == AuthorizeOutcome.ACCEPTED
        assert result["key_shard"] == first
        reservations.append(result["reservation_id"])

    for reservation_id in reservations:
        settled = settle_atomic(
            store._database,
            store._param_types,
            reservation_id=reservation_id,
            actual_micro=900,
            settled_usage_type="Credits",
            success=True,
        )
        assert settled["outcome"] == SettleOutcome.SETTLED

    rows = database.typed[KEY_LIMIT_TABLE]
    assert [rows[(key.hash, shard)]["usage"] for shard in range(4)] == [9_000] * 4
    assert [rows[(key.hash, shard)]["reserved"] for shard in range(4)] == [0] * 4
    assert sum(row["total_usage"] for row in database.typed[CREDIT_BALANCE_TABLE].values()) == 36_000


def test_idempotent_replay_keeps_original_key_shard() -> None:
    store, database, key = _seed(key_shards=4)
    common = {
        "database": store._database,
        "param_types": store._param_types,
        "workspace_id": "ws-key-shards",
        "key_hash": key.hash,
        "estimate": 1_000,
        "has_credit_candidate": True,
        "reservation_usage_type": "Credits",
        "idempotency_scope": "same-key-shard-request",
        "idempotency_fingerprint": "same-body",
        "expires_at": "2026-12-01T00:00:00Z",
        "build_auth_body": _auth_body,
    }

    first = authorize_atomic(**common, key_shard_candidates=(3, 2, 1, 0))
    replay = authorize_atomic(**common, key_shard_candidates=(0, 1, 2, 3))

    assert first["outcome"] == AuthorizeOutcome.ACCEPTED
    assert replay["outcome"] == AuthorizeOutcome.REPLAY
    assert replay["key_shard"] == first["key_shard"] == 3
    assert database.reservations[first["reservation_id"]]["key_shard"] == 3


def test_typed_key_usage_sums_shards_and_current_windows() -> None:
    store, database, key = _seed(key_shards=4)
    floors = window_floors(utcnow())
    rows = database.typed[KEY_LIMIT_TABLE]
    for shard in range(4):
        row = rows[(key.hash, shard)]
        row["usage"] = 10 + shard
        row["byok_usage"] = 2 + shard
        row["reserved"] = shard
        row["day_usage"] = 3 + shard
        row["day_start"] = floors["daily"]
        row["week_usage"] = 4 + shard
        row["week_start"] = floors["weekly"]
        row["month_usage"] = 5 + shard
        row["month_start"] = floors["monthly"]

    usage = store.typed_key_usage(key.hash)

    assert usage == {
        "usage": 46,
        "byok_usage": 14,
        "reserved": 6,
        "windows": {"daily": 18, "weekly": 22, "monthly": 26},
    }


def test_compare_detects_a_missing_configured_key_usage_shard() -> None:
    store, database, key = _seed(key_shards=4)
    assert compare(store).clean

    database.typed[KEY_LIMIT_TABLE].pop((key.hash, 3))
    report = compare(store)

    assert report.key_drift == 1
    assert report.samples[f"api_key:{key.hash}"]["usage_shard_count"] == (4, 3)


def test_deleting_sharded_key_removes_every_typed_usage_row() -> None:
    store, database, key = _seed(key_shards=4)

    assert store.api_keys.delete(key.hash)

    assert not any(row_key == key.hash for row_key, _shard in database.typed[KEY_LIMIT_TABLE])


def test_adding_a_limit_to_sharded_key_is_rejected_atomically() -> None:
    store, _database, key = _seed(key_shards=4)

    with pytest.raises(ValueError, match="consolidate API-key usage"):
        store.api_keys.update(key.hash, {"limit_microdollars": 1_000_000})

    persisted = store.api_keys.get_by_hash(key.hash)
    assert persisted is not None
    assert persisted.limit_microdollars is None
    assert persisted.usage_shard_count == 4


def test_key_usage_operator_split_and_unshard_preserve_all_usage() -> None:
    store, database, key = _seed(key_shards=1)
    floors = window_floors(utcnow())
    row = database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    row.update(
        usage=101,
        byok_usage=37,
        day_usage=19,
        day_start=floors["daily"],
        week_usage=23,
        week_start=floors["weekly"],
        month_usage=29,
        month_start=floors["monthly"],
    )

    split = reshard_key_usage(store, key.hash, 16, apply=True)

    assert split.ready and split.applied
    assert split.current_shard_count == 16
    assert split.usage_micro == 101
    assert split.byok_usage_micro == 37
    rows = [database.typed[KEY_LIMIT_TABLE][(key.hash, shard)] for shard in range(16)]
    assert sum(row["usage"] for row in rows) == 101
    assert sum(row["byok_usage"] for row in rows) == 37
    assert sum(row["day_usage"] for row in rows) == 19
    assert sum(row["week_usage"] for row in rows) == 23
    assert sum(row["month_usage"] for row in rows) == 29
    assert all(row["reserved"] == 0 for row in rows)

    unshard = reshard_key_usage(store, key.hash, 1, apply=True)

    assert unshard.ready and unshard.applied
    [single] = [
        row
        for (row_key, _shard), row in database.typed[KEY_LIMIT_TABLE].items()
        if row_key == key.hash
    ]
    assert single["usage"] == 101
    assert single["byok_usage"] == 37
    assert single["day_usage"] == 19
    assert single["week_usage"] == 23
    assert single["month_usage"] == 29
    persisted = store.api_keys.get_by_hash(key.hash)
    assert persisted.usage_shard_count == 1
    assert persisted.usage_microdollars == 101
    assert persisted.byok_usage_microdollars == 37


def test_key_usage_operator_refuses_capped_or_undrained_key() -> None:
    store, database, key = _seed(key_shards=1)
    key.limit_microdollars = 1_000_000
    store._write_entity("api_key", key.hash, key)

    capped = reshard_key_usage(store, key.hash, 4, apply=True)
    assert not capped.ready
    assert "capped API key must remain on one usage shard" in capped.reasons

    key.limit_microdollars = None
    store._write_entity("api_key", key.hash, key)
    database.reservations["open-key-request"] = {
        "reservation_id": "open-key-request",
        "workspace_id": key.workspace_id,
        "key_hash": key.hash,
        "settled": False,
    }
    undrained = reshard_key_usage(store, key.hash, 4, apply=True)
    assert not undrained.ready
    assert any("open typed reservations" in reason for reason in undrained.reasons)


def test_key_usage_operator_status_is_idempotent_after_split() -> None:
    store, _database, key = _seed(key_shards=1)
    assert reshard_key_usage(store, key.hash, 8, apply=True).applied

    status = inspect_key_usage_reshard(store, key.hash, 8)
    noop = reshard_key_usage(store, key.hash, 8, apply=True)

    assert status.ready
    assert status.current_shard_count == 8
    assert noop.ready
    assert not noop.applied


def test_legacy_rollback_and_shard_zero_repair_refuse_sharded_key_usage() -> None:
    store, _database, key = _seed(key_shards=4)

    backsync = backsync_typed_to_json(store, key.workspace_id, apply=True)
    repair = repair_typed_reserved(store, key.workspace_id, apply=True)

    assert not backsync.ready
    assert "API-key usage is sharded" in backsync.reasons[0]
    assert not repair.ready
    assert any("API-key usage is sharded" in reason for reason in repair.reasons)
