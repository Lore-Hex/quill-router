from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    authorize_atomic,
    settle_atomic,
)
from trusted_router.storage_gcp_counter_reconcile import audit_typed_invariants
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    credit_shard_count,
    distribute_credit_amount,
)
from trusted_router.storage_models import CreditAccount
from trusted_router.typed_balance import live_credit_summary


def _credit_row(
    workspace_id: str,
    shard: int,
    *,
    total_credits: int,
    total_usage: int = 0,
    reserved: int = 0,
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "shard": shard,
        "total_credits": total_credits,
        "total_usage": total_usage,
        "reserved": reserved,
        "source_updated_at": None,
        "updated_at": None,
    }


def _seed_sharded_credit(
    store: object,
    database: object,
    workspace_id: str,
    totals: list[int],
) -> None:
    account = CreditAccount(workspace_id=workspace_id, shard_count=len(totals))
    store._write_entity("credit", workspace_id, account)  # type: ignore[attr-defined]
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})  # type: ignore[attr-defined]
    for shard, total in enumerate(totals):
        table[(workspace_id, shard)] = _credit_row(
            workspace_id, shard, total_credits=total
        )


def _auth_body(authorization_id: str, reservation_id: str) -> str:
    return json.dumps(
        {"id": authorization_id, "credit_reservation_id": reservation_id, "model": "m"}
    )


def test_credit_shard_schema_is_additive_and_rolling_compatible() -> None:
    migration = (
        Path(__file__).parents[1] / "scripts/deploy/migrate_typed_counters.sh"
    ).read_text()

    assert "credit_shard INT64 NOT NULL DEFAULT (0)" in migration
    assert 'ensure_column tr_reservation credit_shard "INT64 DEFAULT (0)"' in migration


def test_legacy_credit_account_defaults_to_exactly_one_shard() -> None:
    account = CreditAccount(workspace_id="ws")

    assert account.shard_count == 1
    assert credit_shard_count(account) == 1
    assert credit_shard_count({"workspace_id": "legacy"}) == 1
    assert distribute_credit_amount(17, 1) == (17,)


@pytest.mark.parametrize("invalid", [0, -1, True])
def test_invalid_credit_shard_count_fails_closed(invalid: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        credit_shard_count({"shard_count": invalid})


def test_credit_grant_distribution_puts_remainder_on_shard_zero() -> None:
    assert distribute_credit_amount(10, 3) == (4, 3, 3)
    assert distribute_credit_amount(-10, 3) == (-4, -3, -3)


def test_authorize_records_credit_shard_and_settle_releases_that_shard() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-shard-plumbing"
    _seed_sharded_credit(store, database, workspace_id, [2_000_000, 2_000_000, 2_000_000])
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="key",
        creator_user_id=None,
        limit_microdollars=6_000_000,
    )

    result = authorize_atomic(
        store._database,
        store._param_types,
        workspace_id=workspace_id,
        key_hash=key.hash,
        estimate=500_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        idempotency_scope="scope-shard-2",
        idempotency_fingerprint="fingerprint",
        expires_at="2026-12-01T00:00:00Z",
        build_auth_body=_auth_body,
        credit_shard=2,
    )

    assert result["outcome"] == AuthorizeOutcome.ACCEPTED
    reservation = database.reservations[result["reservation_id"]]
    assert reservation["credit_shard"] == 2
    assert reservation["ws_shard"] == 2
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 2)]["reserved"] == 500_000
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]["reserved"] == 0

    settled = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=result["reservation_id"],
        actual_micro=450_000,
        settled_usage_type="Credits",
        success=True,
    )

    assert settled["outcome"] == SettleOutcome.SETTLED
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 2)]["reserved"] == 0
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 2)]["total_usage"] == 450_000
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]["total_usage"] == 0


def test_idempotent_replay_keeps_original_credit_shard() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-shard-replay"
    _seed_sharded_credit(store, database, workspace_id, [1_000_000, 1_000_000, 1_000_000])
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="key",
        creator_user_id=None,
        limit_microdollars=3_000_000,
    )
    common = {
        "database": store._database,
        "param_types": store._param_types,
        "workspace_id": workspace_id,
        "key_hash": key.hash,
        "estimate": 250_000,
        "has_credit_candidate": True,
        "reservation_usage_type": "Credits",
        "idempotency_scope": "same-scope",
        "idempotency_fingerprint": "same-fingerprint",
        "expires_at": "2026-12-01T00:00:00Z",
        "build_auth_body": _auth_body,
    }

    first = authorize_atomic(**common, credit_shard=2)
    replay = authorize_atomic(**common, credit_shard=0)

    assert first["outcome"] == AuthorizeOutcome.ACCEPTED
    assert replay["outcome"] == AuthorizeOutcome.REPLAY
    assert replay["reservation_id"] == first["reservation_id"]
    assert database.reservations[first["reservation_id"]]["credit_shard"] == 2
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 2)]["reserved"] == 250_000
    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]["reserved"] == 0


def test_pre_migration_reservation_falls_back_to_ws_shard() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-old-reservation"
    _seed_sharded_credit(store, database, workspace_id, [1_000_000])
    database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]["reserved"] = 200_000
    database.reservations["old-r"] = {
        "reservation_id": "old-r",
        "workspace_id": workspace_id,
        "key_hash": None,
        "ws_shard": 0,
        "credit_shard": None,
        "key_shard": 0,
        "credit_reserved_micro": 200_000,
        "key_reserved_micro": 0,
        "hold_usage_type": "Credits",
        "settled_usage_type": None,
        "actual_micro": None,
        "authorization_id": "old-a",
        "settled": False,
        "expires_at": "2026-12-01T00:00:00Z",
    }

    result = settle_atomic(
        store._database,
        store._param_types,
        reservation_id="old-r",
        actual_micro=150_000,
        settled_usage_type="Credits",
        success=True,
    )

    assert result["outcome"] == SettleOutcome.SETTLED
    row = database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]
    assert row["reserved"] == 0
    assert row["total_usage"] == 150_000


def test_typed_direct_grant_distributes_delta_and_is_idempotent() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-grant-shards"
    _seed_sharded_credit(store, database, workspace_id, [40, 30, 30])

    assert store.credit_workspace_typed_direct(workspace_id, 10, "evt-sharded") is True
    assert store.credit_workspace_typed_direct(workspace_id, 10, "evt-sharded") is False

    rows = database.typed[CREDIT_BALANCE_TABLE]
    assert [rows[(workspace_id, shard)]["total_credits"] for shard in range(3)] == [44, 33, 33]
    assert live_credit_summary(workspace_id, store=store)["total_credits"] == 110
    assert store.get_credit_account(workspace_id).shard_count == 3


def test_typed_direct_grant_rolls_back_when_active_shard_is_missing() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-grant-missing-shard"
    _seed_sharded_credit(store, database, workspace_id, [50, 50])
    del database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 1)]

    with pytest.raises(RuntimeError, match="missing tr_credit_balance shard 1"):
        store.credit_workspace_typed_direct(workspace_id, 10, "evt-missing")

    assert database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]["total_credits"] == 50
    assert store.get_credit_account(workspace_id).shard_count == 2
    assert ("stripe_event", "evt-missing") not in database.rows


def test_typed_credit_snapshot_sums_only_configured_shards() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-summary-shards"
    _seed_sharded_credit(store, database, workspace_id, [50, 30, 20])
    rows = database.typed[CREDIT_BALANCE_TABLE]
    rows[(workspace_id, 0)].update(total_usage=5, reserved=1)
    rows[(workspace_id, 1)].update(total_usage=6, reserved=2)
    rows[(workspace_id, 2)].update(total_usage=7, reserved=3)
    rows[(workspace_id, 3)] = _credit_row(
        workspace_id, 3, total_credits=999, total_usage=999, reserved=999
    )

    assert store.typed_credit_snapshot(workspace_id) == (100, 18, 6)


def test_typed_credit_snapshot_fails_closed_on_missing_configured_shard() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-summary-gap"
    _seed_sharded_credit(store, database, workspace_id, [50, 50])
    del database.typed[CREDIT_BALANCE_TABLE][(workspace_id, 1)]

    with pytest.raises(RuntimeError, match="shard set is incomplete"):
        store.typed_credit_snapshot(workspace_id)


def test_generic_credit_metadata_write_does_not_overwrite_sharded_sub_budgets() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-mirror-shards"
    account = CreditAccount(workspace_id=workspace_id, shard_count=2)

    store._write_entity("credit", workspace_id, account)

    assert database.typed.get(CREDIT_BALANCE_TABLE, {}) == {}


def test_invariant_audit_is_credit_shard_aware() -> None:
    store, database, _ = make_fake_store()
    workspace_id = "ws-audit-shards"
    _seed_sharded_credit(store, database, workspace_id, [60, 40])
    rows = database.typed[CREDIT_BALANCE_TABLE]
    rows[(workspace_id, 0)]["reserved"] = 7
    rows[(workspace_id, 1)]["reserved"] = 11
    database.reservations["legacy"] = {
        "reservation_id": "legacy",
        "workspace_id": workspace_id,
        "key_hash": None,
        "ws_shard": 0,
        "credit_shard": None,
        "credit_reserved_micro": 7,
        "key_reserved_micro": 0,
        "settled": False,
    }
    database.reservations["new"] = {
        "reservation_id": "new",
        "workspace_id": workspace_id,
        "key_hash": None,
        "ws_shard": 1,
        "credit_shard": 1,
        "credit_reserved_micro": 11,
        "key_reserved_micro": 0,
        "settled": False,
    }

    invariant = audit_typed_invariants(store)

    assert invariant.clean, invariant.samples
    assert invariant.credit_rows == 2
