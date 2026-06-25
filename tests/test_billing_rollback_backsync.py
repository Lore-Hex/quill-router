"""Rollback backsync: the inverse of reconcile_for_flip. Before denylisting a
workspace back to legacy, copy the typed gross counters back into JSON so the
legacy path is authoritative + correct (denylist alone is NOT rollback-correct
once typed usage exists). Fail-closed unless the workspace is drained.
"""

from __future__ import annotations

import json

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount, Workspace
from trusted_router.storage_gcp_counter_reconcile import backsync_typed_to_json
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


def _pause(store, ws: str, *, paused: bool = True) -> None:
    store._write_entity("workspace", ws, Workspace(id=ws, name="t", owner_user_id="u", billing_paused=paused))


def _json_credit(db, ws: str) -> dict:
    return json.loads(db.rows[("credit", ws)].body)


def _json_key(db, key_hash: str) -> dict:
    return json.loads(db.rows[("api_key", key_hash)].body)


def test_backsync_copies_typed_gross_into_json_when_drained() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_rollback"
    _pause(store, ws)
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000, total_usage_microdollars=0),
    )
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 400_000, "reserved": 0})
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    db.typed[KEY_LIMIT_TABLE][(key.hash, 0)].update(
        {"usage": 150_000, "byok_usage": 20_000, "reserved": 0}
    )

    assert _json_credit(db, ws)["total_usage_microdollars"] == 0  # stale before

    result = backsync_typed_to_json(store, ws, apply=True)
    assert result.ready and result.applied, result.reasons
    assert result.keys == 1
    # JSON now mirrors the typed gross counters → legacy path is correct on rollback.
    assert _json_credit(db, ws)["total_usage_microdollars"] == 400_000
    assert _json_credit(db, ws)["total_credits_microdollars"] == 1_000_000  # untouched
    assert _json_key(db, key.hash)["usage_microdollars"] == 150_000
    assert _json_key(db, key.hash)["byok_usage_microdollars"] == 20_000


def test_backsync_refuses_open_typed_holds() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_holds"
    _pause(store, ws)
    store._write_entity("credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000))
    db.reservations["r1"] = {
        "reservation_id": "r1", "workspace_id": ws, "credit_reserved_micro": 100_000, "settled": False,
    }

    result = backsync_typed_to_json(store, ws, apply=True)
    assert not result.ready
    assert any("open typed holds" in r for r in result.reasons), result.reasons
    assert not result.applied


def test_backsync_settled_holds_do_not_block() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_settled"
    _pause(store, ws)
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000, total_usage_microdollars=0),
    )
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 200_000, "reserved": 0})
    db.reservations["r1"] = {
        "reservation_id": "r1", "workspace_id": ws, "credit_reserved_micro": 50_000, "settled": True,
    }  # settled → drained

    result = backsync_typed_to_json(store, ws, apply=True)
    assert result.ready and result.applied
    assert _json_credit(db, ws)["total_usage_microdollars"] == 200_000


def test_backsync_assess_only_is_read_only() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_assess"
    _pause(store, ws)
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000, total_usage_microdollars=0),
    )
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 99_000, "reserved": 0})

    result = backsync_typed_to_json(store, ws, apply=False)
    assert result.ready and not result.applied
    assert result.credit == {"total_usage": 99_000, "reserved": 0}
    assert _json_credit(db, ws)["total_usage_microdollars"] == 0  # unchanged by assess


def test_backsync_refuses_unpaused_workspace() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_live"
    _pause(store, ws, paused=False)
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000, total_usage_microdollars=0),
    )
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 300_000, "reserved": 0})

    result = backsync_typed_to_json(store, ws, apply=True)
    assert not result.ready
    assert any("not billing-paused" in r for r in result.reasons), result.reasons
    assert not result.applied
    assert _json_credit(db, ws)["total_usage_microdollars"] == 0  # untouched
