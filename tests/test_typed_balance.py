"""Typed-aware balance reads: typed-capable stores read authoritative typed
counters when a typed row exists; the in-memory store and unseeded typed rows
fall back to the single JSON book.
"""

from __future__ import annotations

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.typed_balance import typed_aware_credit_account


def test_credit_overlay_uses_typed_row_when_store_has_capability() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_typed"
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000, total_usage_microdollars=0),
    )
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws,
        "shard": 0,
        "total_credits": 1_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    # Simulate the typed DML having booked usage + a hold ahead of stale JSON.
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 300_000, "reserved": 150_000})

    typed = typed_aware_credit_account(store, ws, settings=object())
    assert typed.total_credits_microdollars == 1_000_000  # JSON-owned, unchanged
    assert typed.total_usage_microdollars == 300_000
    assert typed.reserved_microdollars == 150_000
    assert typed.workspace_id == ws  # JSON metadata preserved


def test_credit_typed_but_unseeded_falls_back_to_json() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_unseeded"
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=2_000_000, total_usage_microdollars=50_000),
    )
    acct = typed_aware_credit_account(store, ws, settings=object())
    assert acct is not None
    assert acct.total_usage_microdollars == 50_000  # JSON, since no typed row


def test_credit_missing_account_returns_none() -> None:
    store, _db, _ = make_fake_store()
    assert typed_aware_credit_account(store, "nope", settings=object()) is None
