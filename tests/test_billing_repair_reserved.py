"""Repair clobbered typed `reserved` (the 2026-06-25 incident's accumulated
damage): set credit + each key reserved = SUM of that scope's OPEN typed holds,
for a billing-PAUSED already-typed workspace. total_usage is left alone.
"""

from __future__ import annotations

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import Workspace
from trusted_router.storage_gcp_counter_reconcile import repair_typed_reserved
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


def _paused_ws(store, ws: str, *, paused: bool = True) -> None:
    store._write_entity("workspace", ws, Workspace(id=ws, name="t", owner_user_id="u", billing_paused=paused))


def test_repair_sets_reserved_to_open_holds() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_repair"
    _paused_ws(store, ws)
    # clobbered: typed credit reserved=5M, but real open holds = 120k.
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 10_000_000,
        "total_usage": 3_000_000, "reserved": 5_000_000,
    }
    db.reservations["r1"] = {"reservation_id": "r1", "workspace_id": ws, "credit_reserved_micro": 120_000, "settled": False}
    db.reservations["r2"] = {"reservation_id": "r2", "workspace_id": ws, "credit_reserved_micro": 9_000_000, "settled": True}  # settled → ignored
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] = 800_000  # clobbered key reserved
    db.reservations["rk"] = {"reservation_id": "rk", "key_hash": key.hash, "key_reserved_micro": 50_000, "settled": False}

    assessment = repair_typed_reserved(store, ws, apply=False)
    assert assessment.ready and not assessment.applied
    assert assessment.credit_reserved_before == 5_000_000
    assert assessment.credit_reserved_after == 120_000
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 5_000_000  # unchanged by assess

    result = repair_typed_reserved(store, ws, apply=True)
    assert result.ready and result.applied
    assert result.keys_repaired == 1
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 120_000  # = open holds
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 3_000_000  # untouched
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 50_000  # = open key holds


def test_repair_refuses_unpaused() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_live"
    _paused_ws(store, ws, paused=False)
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 1_000_000, "total_usage": 0, "reserved": 99_000,
    }
    result = repair_typed_reserved(store, ws, apply=True)
    assert not result.ready
    assert any("not billing-paused" in r for r in result.reasons), result.reasons
    assert not result.applied
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 99_000  # untouched


def test_repair_aborts_if_a_typed_key_row_is_missing() -> None:
    """codex P1: a key whose typed row is missing (deleted mid-repair) must ABORT
    with ZERO writes — never create a partial, uncapped tr_key_limit row."""
    store, db, _ = make_fake_store()
    ws = "ws_missing_key"
    _paused_ws(store, ws)
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 1_000_000, "total_usage": 0, "reserved": 5_000,
    }
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    del db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]  # typed key row gone

    result = repair_typed_reserved(store, ws, apply=True)
    assert not result.ready and not result.applied
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 5_000  # credit NOT touched (no partial write)
    assert (key.hash, 0) not in db.typed[KEY_LIMIT_TABLE]  # no partial row created


def test_repair_aborts_on_nonzero_shard_holds() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_sharded"
    _paused_ws(store, ws)
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 1_000_000, "total_usage": 0, "reserved": 1234,
    }
    db.reservations["r1"] = {"reservation_id": "r1", "workspace_id": ws, "credit_reserved_micro": 500, "ws_shard": 1, "settled": False}

    result = repair_typed_reserved(store, ws, apply=True)
    assert not result.ready
    assert any("nonzero shard" in r for r in result.reasons), result.reasons
    assert not result.applied
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 1234  # untouched


def test_repair_aborts_on_nonzero_key_shard_holds() -> None:
    """codex round-2 P3: a key hold on a nonzero key_shard (workspace shard 0) would
    be silently omitted from the key reserved SUM and written low — must ABORT."""
    store, db, _ = make_fake_store()
    ws = "ws_key_sharded"
    _paused_ws(store, ws)
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 1_000_000, "total_usage": 0, "reserved": 7_000,
    }
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] = 700  # clobbered key reserved
    # a key hold on a NONZERO key_shard (ws_shard=0) — the ws-level guard misses it.
    db.reservations["rk1"] = {
        "reservation_id": "rk1", "workspace_id": ws, "key_hash": key.hash,
        "key_reserved_micro": 400, "ws_shard": 0, "key_shard": 1, "settled": False,
    }

    result = repair_typed_reserved(store, ws, apply=True)
    assert not result.applied  # aborted in-txn (no write)
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 7_000  # untouched
    assert db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 700  # untouched


def test_repair_zero_holds_zeroes_reserved() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_zero"
    _paused_ws(store, ws)
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 1_000_000, "total_usage": 500_000, "reserved": 29_373,
    }  # clobbered reserved, but NO open holds → should become 0

    result = repair_typed_reserved(store, ws, apply=True)
    assert result.ready and result.applied
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 0
