"""Step 6 of the billing typed-column migration: the ledger-derived FLIP SEED.

reconcile_for_flip() is the sanctioned full-row seed that the ownership-split
mirror/backfill no longer do. It must be FAIL-CLOSED: only a never-typed, fully
drained workspace may be seeded from JSON gross counters (with reserved=0), and a
legacy hold racing the seed must abort it.

See docs/design/billing-typed-counters.md.
"""

from __future__ import annotations

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount
from trusted_router.storage_gcp_counter_reconcile import reconcile_for_flip
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


def _typed_credit(db, ws: str) -> dict:
    return db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]


def _typed_key(db, key_hash: str) -> dict:
    return db.typed[KEY_LIMIT_TABLE][(key_hash, 0)]


def _seed_drained_workspace(store, ws: str, *, total_credits: int, total_usage: int):
    """A never-typed workspace with lifetime usage but no open holds (drained).
    After the ownership split the mirror carries only total_credits, so the typed
    row's total_usage is intentionally stale (0) until reconcile_for_flip seeds it."""
    store._write_entity(
        "credit", ws,
        CreditAccount(
            workspace_id=ws,
            total_credits_microdollars=total_credits,
            total_usage_microdollars=total_usage,
        ),
    )


def test_reconcile_ready_seeds_gross_counters_with_zero_reserved() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_flip"
    _seed_drained_workspace(store, ws, total_credits=5_000_000, total_usage=1_200_000)
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=2_000_000
    )
    store.api_keys.add_usage(key.hash, 300_000, is_byok=False)  # JSON usage only (mirror skips it)

    # Pre-state: the mirror carried total_credits + key config, NOT usage.
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert _typed_key(db, key.hash)["usage"] == 0

    assessment = reconcile_for_flip(store, ws, apply=False)
    assert assessment.ready, assessment.reasons
    assert not assessment.applied  # read-only
    assert _typed_credit(db, ws)["total_usage"] == 0  # unchanged by assessment

    result = reconcile_for_flip(store, ws, apply=True)
    assert result.ready and result.applied, result.reasons
    assert result.keys_seeded == 1
    # Gross counters now seeded from JSON; reserved is 0 (drained, never typed).
    assert _typed_credit(db, ws)["total_credits"] == 5_000_000
    assert _typed_credit(db, ws)["total_usage"] == 1_200_000
    assert _typed_credit(db, ws)["reserved"] == 0
    assert _typed_key(db, key.hash)["usage"] == 300_000
    assert _typed_key(db, key.hash)["reserved"] == 0
    assert _typed_key(db, key.hash)["limit_micro"] == 2_000_000


def test_reconcile_refuses_open_credit_hold() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_hold"
    _seed_drained_workspace(store, ws, total_credits=1_000_000, total_usage=0)
    store.reserve(ws, "key_1", 250_000)  # open legacy credit hold

    result = reconcile_for_flip(store, ws, apply=True)
    assert not result.ready
    assert any("reserved" in r for r in result.reasons), result.reasons
    assert not result.applied
    # No seed written: typed total_usage untouched (still 0 default).
    assert _typed_credit(db, ws)["reserved"] == 0  # never seeded a nonzero reserved


def test_reconcile_refuses_open_key_hold() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_keyhold"
    _seed_drained_workspace(store, ws, total_credits=2_000_000, total_usage=0)
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    store.reserve_key_limit(key.hash, 400_000, usage_type="Credits")  # open legacy key hold

    result = reconcile_for_flip(store, ws, apply=True)
    assert not result.ready
    assert any("reserved" in r for r in result.reasons), result.reasons
    assert not result.applied


def test_reconcile_refuses_typed_history() -> None:
    """A workspace with ANY tr_reservation history already has typed gross
    counters JSON doesn't reflect; a JSON seed would lose typed-era usage."""
    store, db, _ = make_fake_store()
    ws = "ws_typed_hist"
    _seed_drained_workspace(store, ws, total_credits=1_000_000, total_usage=0)
    db.reservations["r_hist"] = {
        "reservation_id": "r_hist", "workspace_id": ws, "settled": True,
    }

    result = reconcile_for_flip(store, ws, apply=True)
    assert not result.ready
    assert any("typed history" in r for r in result.reasons), result.reasons
    assert not result.applied


def test_reconcile_no_partial_seed_when_any_key_has_a_hold() -> None:
    """Multi-key: a hold on ANY key must abort the whole seed with NO partial
    write to the others (returning None commits buffered mutations, so the seed
    must issue none until every predicate passes)."""
    store, db, _ = make_fake_store()
    ws = "ws_partial"
    _seed_drained_workspace(store, ws, total_credits=3_000_000, total_usage=0)
    _raw, k1 = store.api_keys.create(
        workspace_id=ws, name="k1", creator_user_id=None, limit_microdollars=1_000_000
    )
    _raw, k2 = store.api_keys.create(
        workspace_id=ws, name="k2", creator_user_id=None, limit_microdollars=1_000_000
    )
    store.api_keys.add_usage(k1.hash, 100_000, is_byok=False)  # k1 has seedable usage
    store.reserve_key_limit(k2.hash, 200_000, usage_type="Credits")  # k2 has an open hold

    result = reconcile_for_flip(store, ws, apply=True)
    assert not result.ready and not result.applied
    # NOTHING seeded — neither key's usage nor the credit usage moved off 0.
    assert _typed_key(db, k1.hash)["usage"] == 0
    assert _typed_key(db, k2.hash)["usage"] == 0
    assert _typed_credit(db, ws)["total_usage"] == 0


def test_reconcile_refuses_no_account() -> None:
    store, _db, _ = make_fake_store()
    result = reconcile_for_flip(store, "ws_missing", apply=True)
    assert not result.ready
    assert any("no credit account" in r for r in result.reasons), result.reasons
    assert not result.applied
