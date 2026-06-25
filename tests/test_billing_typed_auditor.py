"""The typed-side invariant auditor: every typed `reserved` must equal the sum of
that scope's OPEN typed-origin holds (tr_reservation, settled=false) and be >= 0.
This is the leak detector the narrowed compare() (JSON-vs-typed) can no longer
provide — it catches the exact incident class (a reserved that no longer matches
its outstanding holds).
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
    store, db, _ = make_fake_store()
    ws = "ws_ok"
    _credit_row(db, ws, reserved=300_000)
    _resv(db, "r1", ws=ws, credit=200_000)
    _resv(db, "r2", ws=ws, credit=100_000)
    _resv(db, "r3", ws=ws, credit=999_999, settled=True)  # settled → ignored

    report = audit_typed_invariants(store)
    assert report.clean, (report.summary(), report.samples)
    assert report.credit_rows == 1


def test_auditor_flags_reserved_leak() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_leak"
    _credit_row(db, ws, reserved=500_000)  # but only 100k is actually open
    _resv(db, "r1", ws=ws, credit=100_000)

    report = audit_typed_invariants(store)
    assert not report.clean
    assert report.credit_violations == 1
    assert report.samples[f"credit:{ws}"] == {"typed_reserved": 500_000, "open_holds": 100_000}


def test_auditor_flags_negative_reserved() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_neg"
    _credit_row(db, ws, reserved=-50)  # underflow

    report = audit_typed_invariants(store)
    assert not report.clean
    assert report.credit_violations == 1


def test_auditor_key_invariant() -> None:
    store, db, _ = make_fake_store()
    kh = "key_abc"
    _key_row(db, kh, reserved=250_000)
    _resv(db, "rk", key=kh, key_micro=250_000)

    assert audit_typed_invariants(store).clean

    db.reservations["rk"]["key_reserved_micro"] = 100_000  # now reserved (250k) != open (100k)
    report = audit_typed_invariants(store)
    assert not report.clean
    assert report.key_violations == 1
    assert f"api_key:{kh}" in report.samples
