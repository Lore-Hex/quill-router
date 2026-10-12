"""The typed-side invariant auditor: every typed `reserved` must equal the sum of
that scope's OPEN typed-origin holds (tr_reservation, settled=false) and be >= 0.
This is the standing leak detector for the surviving typed book: it catches the
exact incident class (a reserved that no longer matches its outstanding holds).
"""

from __future__ import annotations

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counter_reconcile import audit_typed_invariants
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


def _credit_row(db, ws: str, reserved: int) -> None:
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 1_000_000,
        "total_usage": 0, "reserved": reserved,
    }


def _key_row(db, key_hash: str, reserved: int) -> None:
    db.typed.setdefault(KEY_LIMIT_TABLE, {})[(key_hash, 0)] = {
        "key_hash": key_hash, "shard": 0, "limit_micro": 1_000_000, "usage": 0,
        "byok_usage": 0, "reserved": reserved, "include_byok": True,
    }


def _resv(db, rid: str, *, ws=None, key=None, credit=0, key_micro=0, settled=False) -> None:
    db.reservations[rid] = {
        "reservation_id": rid, "workspace_id": ws, "key_hash": key,
        "credit_reserved_micro": credit, "key_reserved_micro": key_micro, "settled": settled,
    }


def test_auditor_clean_when_reserved_equals_open_holds() -> None:
    store, db = make_fake_store()
    ws = "ws_ok"
    _credit_row(db, ws, reserved=300_000)
    _resv(db, "r1", ws=ws, credit=200_000)
    _resv(db, "r2", ws=ws, credit=100_000)
    _resv(db, "r3", ws=ws, credit=999_999, settled=True)  # settled → ignored

    report = audit_typed_invariants(store)
    assert report.clean, (report.summary(), report.samples)
    assert report.credit_rows == 1


def test_auditor_flags_reserved_leak() -> None:
    store, db = make_fake_store()
    ws = "ws_leak"
    _credit_row(db, ws, reserved=500_000)  # but only 100k is actually open
    _resv(db, "r1", ws=ws, credit=100_000)

    report = audit_typed_invariants(store)
    assert not report.clean
    assert report.credit_violations == 1
    assert report.samples[f"credit:{ws}:0"] == {"typed_reserved": 500_000, "open_holds": 100_000}


def test_auditor_flags_open_hold_with_no_typed_row() -> None:
    """The dangerous false-negative codex caught: an open hold whose typed row was
    deleted or never created. Iterating only typed rows would miss it; the reverse
    direction catches it."""
    store, db = make_fake_store()
    ws = "ws_orphan_hold"
    _resv(db, "r1", ws=ws, credit=120_000)  # open hold, but NO tr_credit_balance row

    report = audit_typed_invariants(store)
    assert not report.clean
    assert report.credit_violations == 1
    assert report.samples[f"credit-orphan-hold:{ws}:0"] == {
        "typed_reserved": None, "open_holds": 120_000
    }


def test_auditor_flags_negative_reserved() -> None:
    store, db = make_fake_store()
    ws = "ws_neg"
    _credit_row(db, ws, reserved=-50)  # underflow

    report = audit_typed_invariants(store)
    assert not report.clean
    assert report.credit_violations == 1


def test_auditor_key_invariant() -> None:
    store, db = make_fake_store()
    kh = "key_abc"
    _key_row(db, kh, reserved=250_000)
    _resv(db, "rk", key=kh, key_micro=250_000)

    assert audit_typed_invariants(store).clean

    db.reservations["rk"]["key_reserved_micro"] = 100_000  # now reserved (250k) != open (100k)
    report = audit_typed_invariants(store)
    assert not report.clean
    assert report.key_violations == 1
    assert f"api_key:{kh}:0" in report.samples


def _lease(db, lid: str, ws: str, *, state: str = "open", consumed: int = 0, fault: int = 0, donors=()) -> None:
    """A fast-path lease and its donors: each donor (shard, allocation, consumed)."""
    db.fastpath_leases[lid] = {"workspace_id": ws, "state": state, "consumed": consumed, "fault_usage": fault}
    for shard, allocation, booked in donors:
        db.fastpath_lease_donors.append({
            "workspace_id": ws, "lease_id": lid, "credit_shard": shard,
            "allocation": allocation, "consumed": booked,
        })


def test_auditor_counts_a_leases_remaining_allocation_with_the_holds() -> None:
    """§4.7: a shard's reserved is its open request holds plus the remaining
    allocation of its open or draining leases; a closed lease holds nothing."""
    store, db = make_fake_store()
    ws = "ws_leases"
    _credit_row(db, ws, reserved=300_000 + 150_000 + 40_000)
    _resv(db, "r1", ws=ws, credit=300_000)
    _lease(db, "l-open", ws, donors=[(0, 200_000, 50_000)])  # 150k remaining
    _lease(db, "l-draining", ws, state="draining", donors=[(0, 40_000, 0)])
    _lease(db, "l-closed", ws, state="closed", donors=[(0, 90_000, 90_000)])

    report = audit_typed_invariants(store)
    assert report.clean, (report.summary(), report.samples)
    assert report.lease_groups == 1
    assert "lease groups: 1" in report.summary()


def test_auditor_flags_a_reservation_the_leases_and_holds_do_not_explain() -> None:
    store, db = make_fake_store()
    ws = "ws_lease_leak"
    _credit_row(db, ws, reserved=500_000)
    _resv(db, "r1", ws=ws, credit=100_000)
    _lease(db, "l1", ws, donors=[(0, 100_000, 20_000)])  # 80k remaining

    report = audit_typed_invariants(store)
    assert report.credit_violations == 1
    assert report.samples[f"credit:{ws}:0"] == {"typed_reserved": 500_000, "open_holds": 180_000}


def test_auditor_flags_a_lease_allocation_on_a_shard_with_no_typed_row() -> None:
    store, db = make_fake_store()
    ws = "ws_lease_orphan"
    _credit_row(db, ws, reserved=0)
    _lease(db, "l1", ws, donors=[(1, 70_000, 0)])  # shard 1 has no typed row

    report = audit_typed_invariants(store)
    assert report.credit_violations == 1
    assert report.samples[f"credit-orphan-hold:{ws}:1"] == {"typed_reserved": None, "open_holds": 70_000}


def test_auditor_counts_lease_bookings_as_booked_usage() -> None:
    """A lease's bookings raise total_usage with no reservation row behind
    them, a closed lease's too: the usage arm counts them with the settled
    actuals, so a counter that equals both is reconciled, not unauditable."""
    store, db = make_fake_store()
    ws = "ws_lease_usage"
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 1_000_000,
        "total_usage": 120_000 + 30_000, "reserved": 0,
    }
    _resv(db, "r1", ws=ws, credit=0, settled=True)
    db.reservations["r1"].update({"actual_micro": 120_000, "settled_usage_type": "Credits"})
    # 30k booked: 20k on the donors and 10k past them, as fault usage.
    _lease(db, "l-closed", ws, state="closed", consumed=20_000, fault=10_000, donors=[(0, 20_000, 20_000)])

    report = audit_typed_invariants(store)
    assert report.clean and report.fully_audited, (report.summary(), report.samples, report.unauditable)
    assert report.lease_groups == 0

    # A counter below the settled actuals plus the leases' bookings, by the
    # same amount in both snapshots, is behind its ledger: the second read
    # counts the leases too, or the gap would seem to have closed.
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] = 120_000
    report = audit_typed_invariants(store)
    assert report.usage_behind_ledger == 1, (report.summary(), report.unauditable)
    assert report.unauditable[f"usage-behind-ledger:{ws}"]["shortfall"] == 30_000


def test_auditor_flags_lease_bookings_of_a_workspace_with_no_typed_row() -> None:
    store, db = make_fake_store()
    _lease(db, "l1", "ws_no_row", state="closed", consumed=5_000, donors=[(0, 5_000, 5_000)])

    report = audit_typed_invariants(store)
    assert report.usage_violations == 1
    assert report.samples["usage-orphan-booking:ws_no_row"]["lease_bookings"] == 5_000
