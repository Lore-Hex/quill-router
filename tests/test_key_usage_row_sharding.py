from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    authorize_atomic,
    check_key_window_limits,
    settle_atomic,
)
from trusted_router.storage_gcp_counter_reconcile import (
    repair_typed_reserved,
)
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    KEY_LIMIT_TABLE,
    key_usage_shard_count,
)
from trusted_router.storage_gcp_credit_shard_admin import reshard_credit_account
from trusted_router.storage_gcp_key_shard_admin import (
    inspect_key_usage_reshard,
    reshard_key_usage,
)
from trusted_router.storage_models import CreditAccount, Reservation, Workspace


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
        CreditAccount(workspace_id=workspace_id),
    )
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace_id, 0)] = {
        "workspace_id": workspace_id,
        "shard": 0,
        "total_credits": 1_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="uncapped-sharded-key",
        creator_user_id=None,
        limit_microdollars=None,
    )
    key.usage_shard_count = key_shards
    store._write_entity("api_key", key.hash, key)
    rows = database.typed.setdefault(KEY_LIMIT_TABLE, {})
    for shard in range(key_shards):
        rows.setdefault(
            (key.hash, shard),
            {
                "key_hash": key.hash,
                "shard": shard,
                "limit_micro": None,
                "usage": 0,
                "byok_usage": 0,
                "reserved": 0,
                "include_byok": True,
                "day_limit_micro": None,
                "week_limit_micro": None,
                "month_limit_micro": None,
                "day_usage": 0,
                "day_start": None,
                "week_usage": 0,
                "week_start": None,
                "month_usage": 0,
                "month_start": None,
                "source_updated_at": None,
                "updated_at": None,
            },
        )
    return store, database, key


def _auth_body(authorization_id: str, reservation_id: str) -> str:
    return json.dumps(
        {"id": authorization_id, "credit_reservation_id": reservation_id}
    )


def test_key_usage_shards_default_and_fail_closed_for_lifetime_caps() -> None:
    assert key_usage_shard_count({}) == 1
    assert key_usage_shard_count({"usage_shard_count": 16}) == 16
    with pytest.raises(ValueError, match="positive integer"):
        key_usage_shard_count({"usage_shard_count": 0})
    with pytest.raises(ValueError, match="must not exceed"):
        key_usage_shard_count({"usage_shard_count": 65})
    with pytest.raises(ValueError, match="exact lifetime limit"):
        key_usage_shard_count(
            {"usage_shard_count": 2, "limit_microdollars": 1_000_000}
        )
    assert (
        key_usage_shard_count(
            {"usage_shard_count": 2, "limit_daily_microdollars": 1_000_000}
        )
        == 2
    )


def test_new_uncapped_key_inherits_workspace_credit_shards() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-new-key-shards"
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id, shard_count=16),
    )

    _raw, key = store.create_api_key(
        workspace_id=workspace_id,
        name="inherits-workspace-scale",
        creator_user_id=None,
    )

    assert key.usage_shard_count == 16
    assert {
        shard
        for key_hash, shard in database.typed[KEY_LIMIT_TABLE]
        if key_hash == key.hash
    } == set(range(16))


def test_new_lifetime_capped_key_remains_single_shard() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-new-capped-key"
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id, shard_count=16),
    )

    _raw, key = store.create_api_key(
        workspace_id=workspace_id,
        name="exact-cap",
        creator_user_id=None,
        limit_microdollars=1_000_000,
    )

    assert key.usage_shard_count == 1
    assert {
        shard
        for key_hash, shard in database.typed[KEY_LIMIT_TABLE]
        if key_hash == key.hash
    } == {0}


def test_sharded_key_metadata_update_does_not_clobber_typed_counters() -> None:
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


def test_typed_key_usage_fails_closed_on_missing_configured_shard() -> None:
    store, database, key = _seed(key_shards=4)

    database.typed[KEY_LIMIT_TABLE].pop((key.hash, 3))

    with pytest.raises(RuntimeError, match="usage shard set is incomplete"):
        store.typed_key_usage(key.hash)


def test_deleting_sharded_key_leaves_typed_usage_rows() -> None:
    store, database, key = _seed(key_shards=4)

    assert store.api_keys.delete(key.hash)

    assert ("api_key", key.hash) not in database.rows
    assert {(key.hash, shard) for shard in range(4)} <= set(database.typed[KEY_LIMIT_TABLE])


def test_adding_a_limit_to_sharded_key_is_rejected_atomically() -> None:
    store, _database, key = _seed(key_shards=4)

    with pytest.raises(ValueError, match="consolidate API-key usage"):
        store.api_keys.update(key.hash, {"limit_microdollars": 1_000_000})

    persisted = store.api_keys.get_by_hash(key.hash)
    assert persisted is not None
    assert persisted.limit_microdollars is None
    assert persisted.usage_shard_count == 4


def test_adding_window_limits_to_sharded_key_preserves_usage_rows() -> None:
    store, database, key = _seed(key_shards=4)
    rows = database.typed[KEY_LIMIT_TABLE]
    rows[(key.hash, 2)]["usage"] = 123
    rows[(key.hash, 2)]["day_usage"] = 45
    rows[(key.hash, 2)]["day_start"] = window_floors(utcnow())["daily"]

    updated = store.api_keys.update(
        key.hash,
        {
            "limit_daily_microdollars": 1_000_000,
            "limit_weekly_microdollars": 4_000_000,
        },
    )

    assert updated is not None
    assert updated.usage_shard_count == 4
    for shard in range(4):
        assert rows[(key.hash, shard)]["day_limit_micro"] == 1_000_000
        assert rows[(key.hash, shard)]["week_limit_micro"] == 4_000_000
    assert rows[(key.hash, 2)]["usage"] == 123
    assert rows[(key.hash, 2)]["day_usage"] == 45


def test_window_limit_check_sums_all_usage_shards_and_fails_closed() -> None:
    store, database, key = _seed(key_shards=4)
    floors = window_floors(utcnow())
    rows = database.typed[KEY_LIMIT_TABLE]
    for shard, usage in enumerate((200, 250, 300, 150)):
        rows[(key.hash, shard)]["day_usage"] = usage
        rows[(key.hash, shard)]["day_start"] = floors["daily"]

    assert (
        check_key_window_limits(
            store._database,
            store._param_types,
            key_hash=key.hash,
            estimate=101,
            window_limits={"daily": 1_000},
            shard_count=4,
        )
        == "daily"
    )
    assert (
        check_key_window_limits(
            store._database,
            store._param_types,
            key_hash=key.hash,
            estimate=100,
            window_limits={"daily": 1_000},
            shard_count=4,
        )
        is None
    )

    rows.pop((key.hash, 3))
    with pytest.raises(RuntimeError, match="usage shard set is incomplete"):
        check_key_window_limits(
            store._database,
            store._param_types,
            key_hash=key.hash,
            estimate=1,
            window_limits={"daily": 1_000},
            shard_count=4,
        )
    with pytest.raises(ValueError, match="shard_count must be positive"):
        check_key_window_limits(
            store._database,
            store._param_types,
            key_hash=key.hash,
            estimate=1,
            window_limits={"daily": 1_000},
            shard_count=0,
        )


def test_typed_gateway_authorize_applies_window_limit_across_shards() -> None:
    store, database, key = _seed(key_shards=4)
    floors = window_floors(utcnow())
    rows = database.typed[KEY_LIMIT_TABLE]
    for shard in range(4):
        rows[(key.hash, shard)]["day_usage"] = 250
        rows[(key.hash, shard)]["day_start"] = floors["daily"]

    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        estimate=1,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="test/model",
        provider="test",
        requested_model_id="test/model",
        candidate_model_ids=["test/model"],
        region="test",
        endpoint_id="test-endpoint",
        candidate_endpoint_ids=["test-endpoint"],
        idempotency_key="window-shard-limit",
        idempotency_fingerprint="same-body",
        key_usage_shards=4,
        window_limits={"daily": 1_000},
    )

    assert outcome == f"{AuthorizeOutcome.KEY_WINDOW_LIMIT_EXCEEDED}:daily"
    assert authorization is None


def test_key_usage_operator_split_and_unshard_preserve_all_usage() -> None:
    store, database, key = _seed(key_shards=1)
    key.limit_daily_microdollars = 1_000_000
    key.limit_weekly_microdollars = 4_000_000
    key.limit_monthly_microdollars = 10_000_000
    store._write_entity("api_key", key.hash, key)
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
    assert all(row["day_limit_micro"] == 1_000_000 for row in rows)
    assert all(row["week_limit_micro"] == 4_000_000 for row in rows)
    assert all(row["month_limit_micro"] == 10_000_000 for row in rows)

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


def test_online_credit_and_key_split_preserve_then_settle_live_request() -> None:
    store, database, key = _seed(key_shards=1)
    authorized = authorize_atomic(
        store._database,
        store._param_types,
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        estimate=1_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        idempotency_scope="online-reshard-live",
        idempotency_fingerprint="same-body",
        expires_at="2026-12-01T00:00:00Z",
        build_auth_body=_auth_body,
        credit_shard_candidates=(0,),
        key_shard_candidates=(0,),
    )
    assert authorized["outcome"] == AuthorizeOutcome.ACCEPTED
    reservation_id = authorized["reservation_id"]

    credit_split = reshard_credit_account(
        store,
        key.workspace_id,
        4,
        apply=True,
        preserve_open_holds=True,
    )
    key_split = reshard_key_usage(
        store,
        key.hash,
        4,
        apply=True,
        preserve_open_holds=True,
    )

    assert credit_split.ready and credit_split.applied
    assert key_split.ready and key_split.applied
    credit_rows = database.typed[CREDIT_BALANCE_TABLE]
    key_rows = database.typed[KEY_LIMIT_TABLE]
    assert [credit_rows[(key.workspace_id, shard)]["reserved"] for shard in range(4)] == [
        1_000,
        0,
        0,
        0,
    ]
    assert [key_rows[(key.hash, shard)]["reserved"] for shard in range(4)] == [
        0,
        0,
        0,
        0,
    ]

    settled = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=reservation_id,
        actual_micro=900,
        settled_usage_type="Credits",
        success=True,
    )

    assert settled["outcome"] == SettleOutcome.SETTLED
    assert sum(row["reserved"] for row in credit_rows.values()) == 0
    assert sum(row["total_usage"] for row in credit_rows.values()) == 900
    assert sum(row["usage"] for row in key_rows.values()) == 900
    assert database.reservations[reservation_id]["settled"] is True


def test_online_key_split_keeps_history_and_windows_on_existing_shard() -> None:
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

    split = reshard_key_usage(
        store,
        key.hash,
        4,
        apply=True,
        preserve_open_holds=True,
    )

    assert split.ready and split.applied
    rows = [database.typed[KEY_LIMIT_TABLE][(key.hash, shard)] for shard in range(4)]
    assert [row["usage"] for row in rows] == [101, 0, 0, 0]
    assert [row["byok_usage"] for row in rows] == [37, 0, 0, 0]
    assert [row["day_usage"] for row in rows] == [19, 0, 0, 0]
    assert [row["week_usage"] for row in rows] == [23, 0, 0, 0]
    assert [row["month_usage"] for row in rows] == [29, 0, 0, 0]


def test_key_reshard_derives_window_floors_inside_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed(key_shards=1)
    before_boundary = dt.datetime(2026, 7, 28, 23, 59, tzinfo=dt.UTC)
    after_boundary = dt.datetime(2026, 7, 29, 0, 0, tzinfo=dt.UTC)
    current_floors = window_floors(after_boundary)
    key.limit_daily_microdollars = 19
    store._write_entity("api_key", key.hash, key)
    row = database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    row.update(
        day_usage=19,
        day_start=current_floors["daily"],
    )
    transaction_started = False

    def boundary_now() -> dt.datetime:
        return after_boundary if transaction_started else before_boundary

    original_run_in_transaction = store._run_in_transaction

    def run_after_boundary(func: Any, *, attempts: int = 8) -> Any:
        nonlocal transaction_started
        transaction_started = True
        return original_run_in_transaction(func, attempts=attempts)

    monkeypatch.setattr(
        "trusted_router.storage_gcp_key_shard_admin.utcnow",
        boundary_now,
    )
    monkeypatch.setattr(
        "trusted_router.storage_gcp_authorize.utcnow",
        lambda: after_boundary,
    )
    monkeypatch.setattr(store, "_run_in_transaction", run_after_boundary)

    split = reshard_key_usage(store, key.hash, 4, apply=True)

    assert split.ready and split.applied
    rows = [
        database.typed[KEY_LIMIT_TABLE][(key.hash, shard)]
        for shard in range(4)
    ]
    assert sum(current["day_usage"] for current in rows) == 19
    assert all(
        current["day_start"] == current_floors["daily"] for current in rows
    )
    assert (
        check_key_window_limits(
            store._database,
            store._param_types,
            key_hash=key.hash,
            estimate=1,
            window_limits={"daily": 19},
            shard_count=4,
        )
        == "daily"
    )


def test_key_usage_operator_refuses_lifetime_capped_or_undrained_key() -> None:
    store, database, key = _seed(key_shards=1)
    key.limit_microdollars = 1_000_000
    store._write_entity("api_key", key.hash, key)

    capped = reshard_key_usage(store, key.hash, 4, apply=True)
    assert not capped.ready
    assert (
        "API key with an exact lifetime limit must remain on one usage shard"
        in capped.reasons
    )

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


def test_key_reshard_ignores_but_reports_retained_stale_legacy_hold() -> None:
    store, _database, key = _seed(key_shards=1)
    store._write_entity(
        "reservation",
        "legacy-stale-key",
        Reservation(
            id="legacy-stale-key",
            workspace_id=key.workspace_id,
            key_hash=key.hash,
            amount_microdollars=1,
            created_at=(
                dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
            ).isoformat(),
        ),
    )

    result = reshard_key_usage(store, key.hash, 4, apply=True)

    assert result.ready and result.applied
    assert result.legacy_open_reservations == 0
    assert result.stale_legacy_reservations_ignored == 1


def test_key_usage_operator_status_is_idempotent_after_split() -> None:
    store, _database, key = _seed(key_shards=1)
    assert reshard_key_usage(store, key.hash, 8, apply=True).applied

    status = inspect_key_usage_reshard(store, key.hash, 8)
    noop = reshard_key_usage(store, key.hash, 8, apply=True)

    assert status.ready
    assert status.current_shard_count == 8
    assert noop.ready
    assert not noop.applied


def test_shard_zero_repair_refuses_sharded_key_usage() -> None:
    store, _database, key = _seed(key_shards=4)

    repair = repair_typed_reserved(store, key.workspace_id, apply=True)

    assert not repair.ready
    assert any("API-key usage is sharded" in reason for reason in repair.reasons)
