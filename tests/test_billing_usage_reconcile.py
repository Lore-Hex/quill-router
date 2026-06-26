"""Post-flip usage reconcile: set typed credit total_usage = JSON.total_usage +
Σ settled-Credits actual_micro (the ledger), for a billing-PAUSED workspace.

Fixes residual usage drift after the universal typed flip — an ever-typed leftover
whose typed usage froze stale, or a new workspace that straddled legacy+typed
during the cross-region rollout. Leaves reserved + keys untouched.
"""

from __future__ import annotations

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount, Workspace
from trusted_router.storage_gcp_counter_reconcile import reconcile_typed_credit_usage
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _paused_ws(store, ws: str, *, json_usage: int, paused: bool = True, total_credits: int = 10_000_000) -> None:
    store._write_entity("workspace", ws, Workspace(id=ws, name="t", owner_user_id="u", billing_paused=paused))
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=total_credits, total_usage_microdollars=json_usage),
    )


def _typed(db, ws: str, *, total_usage: int) -> None:
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 10_000_000, "total_usage": total_usage, "reserved": 0,
    }


def test_usage_reconcile_sets_typed_usage_to_json_plus_ledger() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_straddle"
    _paused_ws(store, ws, json_usage=10_000)        # legacy-era baseline
    _typed(db, ws, total_usage=5_000)               # straddle: only typed-era counted
    # ledger = Σ settled-Credits actuals = 5000; BYOK + open holds excluded.
    db.reservations["r1"] = {"reservation_id": "r1", "workspace_id": ws, "settled": True, "settled_usage_type": "Credits", "actual_micro": 3_000, "ws_shard": 0}
    db.reservations["r2"] = {"reservation_id": "r2", "workspace_id": ws, "settled": True, "settled_usage_type": "Credits", "actual_micro": 2_000, "ws_shard": 0}
    db.reservations["r3"] = {"reservation_id": "r3", "workspace_id": ws, "settled": True, "settled_usage_type": "BYOK", "actual_micro": 9_999, "ws_shard": 0}
    db.reservations["r4"] = {"reservation_id": "r4", "workspace_id": ws, "settled": False, "credit_reserved_micro": 1_000, "ws_shard": 0}

    a = reconcile_typed_credit_usage(store, ws, apply=False)
    assert a.ready and not a.applied
    assert a.usage_before == 5_000
    assert a.usage_after == 15_000  # 10000 JSON + 5000 ledger (BYOK + open excluded)
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 5_000  # unchanged by assess

    r = reconcile_typed_credit_usage(store, ws, apply=True)
    assert r.ready and r.applied
    assert r.usage_after == 15_000
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 15_000
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] == 0  # untouched


def test_usage_reconcile_refuses_unpaused() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_live"
    _paused_ws(store, ws, json_usage=10_000, paused=False)
    _typed(db, ws, total_usage=5_000)
    r = reconcile_typed_credit_usage(store, ws, apply=True)
    assert not r.ready and not r.applied
    assert any("not billing-paused" in x for x in r.reasons), r.reasons
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 5_000


def test_usage_reconcile_aborts_on_nonzero_shard_credits_actual() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_sharded"
    _paused_ws(store, ws, json_usage=10_000)
    _typed(db, ws, total_usage=5_000)
    db.reservations["r1"] = {"reservation_id": "r1", "workspace_id": ws, "settled": True, "settled_usage_type": "Credits", "actual_micro": 3_000, "ws_shard": 1}
    r = reconcile_typed_credit_usage(store, ws, apply=True)
    assert not r.ready
    assert any("nonzero shard" in x for x in r.reasons), r.reasons
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 5_000


# NOTE: the "abort if the typed credit row is missing" path is correct for prod
# (real Spanner returns [] for a missing row -> abort, never create), but the fake
# returns a default-0 row for a missing tr_credit_balance point-read, so it can't be
# exercised here. In prod every workspace has a mirror-created typed credit row, so
# this path is defensive only. (Fake fidelity gap tracked with the single-use
# snapshot one.)
