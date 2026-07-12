from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_credit_shard_admin import (
    inspect_credit_reshard,
    reshard_credit_account,
)
from trusted_router.storage_gcp_credit_shards import CreditShardCountCache
from trusted_router.storage_models import CreditAccount, Reservation, Workspace


def _seed(
    *,
    shard_credits: list[int],
    shard_usage: list[int],
    shard_reserved: list[int] | None = None,
    paused: bool = True,
    workspace_id: str = "ws-reshard",
) -> tuple[Any, Any]:
    store, database, _ = make_fake_store()
    shard_reserved = shard_reserved or [0] * len(shard_credits)
    store._write_entity(
        "workspace",
        workspace_id,
        Workspace(
            id=workspace_id,
            name="Reshard test",
            owner_user_id="owner",
            billing_paused=paused,
        ),
    )
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            total_credits_microdollars=sum(shard_credits),
            total_usage_microdollars=sum(shard_usage),
            reserved_microdollars=sum(shard_reserved),
            shard_count=len(shard_credits),
        ),
    )
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, credits in enumerate(shard_credits):
        table[(workspace_id, shard)] = {
            "workspace_id": workspace_id,
            "shard": shard,
            "total_credits": credits,
            "total_usage": shard_usage[shard],
            "reserved": shard_reserved[shard],
            "source_updated_at": None,
            "updated_at": None,
        }
    return store, database


def _rows(database: Any, workspace_id: str = "ws-reshard") -> list[dict[str, Any]]:
    table = database.typed[CREDIT_BALANCE_TABLE]
    return [
        table[(workspace_id, shard)]
        for shard in sorted(key[1] for key in table if key[0] == workspace_id)
    ]


def test_dry_run_is_read_only_and_requires_pause() -> None:
    store, database = _seed(shard_credits=[101], shard_usage=[37], paused=False)
    before_rows = [dict(row) for row in _rows(database)]

    result = reshard_credit_account(store, "ws-reshard", 4, apply=False)

    assert not result.ready
    assert result.applied is False
    assert "workspace not billing-paused" in result.reasons
    assert _rows(database) == before_rows
    assert store.get_credit_account("ws-reshard").shard_count == 1


def test_split_is_atomic_even_partitioned_and_invalidates_cached_count() -> None:
    store, database = _seed(shard_credits=[101], shard_usage=[37])
    store._credit_shard_counts = CreditShardCountCache(ttl_seconds=600)
    assert store._credit_shard_counts.get("ws-reshard", lambda: 1) == 1

    result = reshard_credit_account(store, "ws-reshard", 4, apply=True)

    assert result.ready
    assert result.applied
    assert result.current_shard_count == 4
    rows = _rows(database)
    assert [row["total_credits"] for row in rows] == [26, 25, 25, 25]
    assert [row["total_usage"] for row in rows] == [10, 9, 9, 9]
    assert [row["reserved"] for row in rows] == [0, 0, 0, 0]
    assert sum(row["total_credits"] for row in rows) == 101
    assert sum(row["total_usage"] for row in rows) == 37
    account = store.get_credit_account("ws-reshard")
    assert account.shard_count == 4
    assert account.total_credits_microdollars == 101
    assert account.total_usage_microdollars == 37
    assert store._credit_shard_count("ws-reshard") == 4


def test_split_then_unshard_round_trip_preserves_every_global_counter() -> None:
    store, database = _seed(shard_credits=[101], shard_usage=[37])
    split = reshard_credit_account(store, "ws-reshard", 16, apply=True)
    assert split.ready and split.applied

    unshard = reshard_credit_account(store, "ws-reshard", 1, apply=True)

    assert unshard.ready and unshard.applied
    rows = _rows(database)
    assert rows == [
        {
            "workspace_id": "ws-reshard",
            "shard": 0,
            "total_credits": 101,
            "total_usage": 37,
            "reserved": 0,
            "source_updated_at": store._spanner.COMMIT_TIMESTAMP,
            "updated_at": store._spanner.COMMIT_TIMESTAMP,
        }
    ]
    account = store.get_credit_account("ws-reshard")
    assert account.shard_count == 1
    assert account.total_credits_microdollars == 101
    assert account.total_usage_microdollars == 37


def test_same_target_is_verified_idempotent_noop() -> None:
    store, database = _seed(
        shard_credits=[40, 30, 30],
        shard_usage=[4, 3, 3],
    )
    before = [dict(row) for row in _rows(database)]

    result = reshard_credit_account(store, "ws-reshard", 3, apply=True)

    assert result.ready
    assert result.applied is False
    assert _rows(database) == before


@pytest.mark.parametrize(
    ("reserved", "typed_open", "legacy_open"),
    [
        (10, False, False),
        (0, True, False),
        (0, False, True),
    ],
)
def test_reshard_refuses_any_undrained_hold(
    reserved: int,
    typed_open: bool,
    legacy_open: bool,
) -> None:
    store, database = _seed(
        shard_credits=[100],
        shard_usage=[20],
        shard_reserved=[reserved],
    )
    if typed_open:
        database.reservations["typed-open"] = {
            "reservation_id": "typed-open",
            "workspace_id": "ws-reshard",
            "settled": False,
        }
    if legacy_open:
        store._write_entity(
            "reservation",
            "legacy-open",
                Reservation(
                    id="legacy-open",
                    workspace_id="ws-reshard",
                    key_hash="key",
                    amount_microdollars=1,
            ),
        )
    before = [dict(row) for row in _rows(database)]

    result = reshard_credit_account(store, "ws-reshard", 4, apply=True)

    assert not result.ready
    assert not result.applied
    assert any("drain" in reason for reason in result.reasons)
    assert _rows(database) == before
    assert store.get_credit_account("ws-reshard").shard_count == 1


def test_reshard_refuses_incomplete_rows_and_deposited_credit_drift() -> None:
    store, database = _seed(
        shard_credits=[50, 50],
        shard_usage=[10, 10],
    )
    database.typed[CREDIT_BALANCE_TABLE].pop(("ws-reshard", 1))

    incomplete = reshard_credit_account(store, "ws-reshard", 4, apply=True)
    assert not incomplete.ready
    assert "configured typed credit shard set is incomplete" in incomplete.reasons

    database.typed[CREDIT_BALANCE_TABLE][("ws-reshard", 1)] = {
        "workspace_id": "ws-reshard",
        "shard": 1,
        "total_credits": 40,
        "total_usage": 10,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    drift = reshard_credit_account(store, "ws-reshard", 4, apply=True)
    assert not drift.ready
    assert any(
        reason.startswith("typed/JSON deposited-credit totals differ")
        for reason in drift.reasons
    )
    assert store.get_credit_account("ws-reshard").shard_count == 2


def test_reshard_refreshes_stale_json_usage_from_authoritative_typed_sum() -> None:
    store, _database = _seed(shard_credits=[100], shard_usage=[60])
    account = store.get_credit_account("ws-reshard")
    account.total_usage_microdollars = 1
    store._write_entity("credit", "ws-reshard", account)

    result = reshard_credit_account(store, "ws-reshard", 2, apply=True)

    assert result.ready and result.applied
    refreshed = store.get_credit_account("ws-reshard")
    assert refreshed.total_usage_microdollars == 60
    assert refreshed.shard_count == 2


def test_post_commit_inspection_reports_exact_partition() -> None:
    store, _database = _seed(shard_credits=[75], shard_usage=[25])
    assert reshard_credit_account(store, "ws-reshard", 8, apply=True).applied

    status = inspect_credit_reshard(store, "ws-reshard", 8)

    assert status.ready
    assert status.current_shard_count == 8
    assert status.total_credits_micro == 75
    assert status.total_usage_micro == 25
    assert status.reserved_micro == 0


def test_transaction_rechecks_drain_after_preflight_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database = _seed(shard_credits=[100], shard_usage=[20])
    before = [dict(row) for row in _rows(database)]
    original_run = store._run_in_transaction

    def inject_open_hold(callback: Any, *, attempts: int = 8) -> Any:
        database.reservations["arrived-after-preflight"] = {
            "reservation_id": "arrived-after-preflight",
            "workspace_id": "ws-reshard",
            "settled": False,
        }
        return original_run(callback, attempts=attempts)

    monkeypatch.setattr(store, "_run_in_transaction", inject_open_hold)

    result = reshard_credit_account(store, "ws-reshard", 4, apply=True)

    assert not result.ready
    assert not result.applied
    assert "atomic reshard preconditions changed" in result.reasons[0]
    assert _rows(database) == before
    assert store.get_credit_account("ws-reshard").shard_count == 1
