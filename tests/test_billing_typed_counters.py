"""Billing typed-column migration: the JSON->typed mirror and its OWNERSHIP SPLIT.

The mirror propagates only the columns JSON owns — total_credits (credit) and
limit_micro / include_byok (key config). reserved + total_usage (credit) and
usage / byok_usage / reserved (key) are owned by the typed authorize/finalize
DML; the mirror must NOT write them or it clobbers an in-flight typed hold (the
2026-06-25 "typed finalize failed: release row-count != 1" incident).

These tests prove total_credits/config still track JSON, that a JSON write can
never clobber a typed-DML-owned hold, and that the drift comparator only flags
JSON-owned columns.

See docs/design/billing-typed-counters.md.
"""

from __future__ import annotations

import json

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount
from trusted_router.storage_gcp_counter_dml import (
    release_credit,
    reserve_credit,
    reserve_key,
)
from trusted_router.storage_gcp_counter_reconcile import backfill, compare
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    KEY_LIMIT_TABLE,
    credit_drift,
    key_drift,
)


def _json_credit(db, workspace_id: str) -> dict:
    return json.loads(db.rows[("credit", workspace_id)].body)


def _json_key(db, key_hash: str) -> dict:
    return json.loads(db.rows[("api_key", key_hash)].body)


def _typed_credit(db, workspace_id: str) -> dict:
    return db.typed[CREDIT_BALANCE_TABLE][(workspace_id, 0)]


def _typed_key(db, key_hash: str) -> dict:
    return db.typed[KEY_LIMIT_TABLE][(key_hash, 0)]


def assert_credit_total_mirrored(db, workspace_id: str) -> None:
    """The mirror propagates ONLY the JSON-owned total_credits. reserved +
    total_usage are typed-DML-owned and are deliberately NOT sourced from JSON."""
    j = _json_credit(db, workspace_id)
    t = _typed_credit(db, workspace_id)
    assert t["total_credits"] == j["total_credits_microdollars"], (j, t)
    assert t["shard"] == 0


def assert_key_config_mirrored(db, key_hash: str) -> None:
    """The mirror propagates ONLY the JSON-owned config (limit_micro,
    include_byok). usage / byok_usage / reserved are typed-DML-owned."""
    j = _json_key(db, key_hash)
    t = _typed_key(db, key_hash)
    assert t["limit_micro"] == j["limit_microdollars"], (j, t)
    assert t["include_byok"] == j["include_byok_in_limit"], (j, t)
    assert t["shard"] == 0


def test_credit_total_credits_is_mirrored() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_mirror"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    assert_credit_total_mirrored(db, ws)
    assert _typed_credit(db, ws)["total_credits"] == 1_000_000
    # A later credit event (top-up) re-mirrors the new total_credits.
    store.credit_workspace_once(ws, 500_000, "evt_topup")
    assert _typed_credit(db, ws)["total_credits"] == 1_500_000


def test_json_credit_reserve_does_not_propagate_to_typed_reserved() -> None:
    """A legacy JSON-path reserve updates JSON.reserved, but the mirror does NOT
    carry it into the typed-DML-owned tr_credit_balance.reserved."""
    store, db, _ = make_fake_store()
    ws = "ws_legacy_reserve"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    store.reserve(ws, "key_1", 250_000)
    assert _json_credit(db, ws)["reserved_microdollars"] == 250_000
    # total_credits still mirrored; reserved stays at the typed default (0).
    assert_credit_total_mirrored(db, ws)
    assert _typed_credit(db, ws)["reserved"] == 0


def test_json_credit_write_does_not_clobber_typed_hold() -> None:
    """THE 2026-06-25 incident reproduction. A typed-DML hold sits in
    tr_credit_balance.reserved; a JSON credit write (top-up) fires the mirror.
    Before the ownership split the mirror overwrote reserved with the stale JSON
    value (0), so the next typed finalize failed 'release row-count != 1'. Now
    the hold is untouched and the release succeeds."""
    store, db, _ = make_fake_store()
    ws = "ws_clobber"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    pt = store._param_types
    # take a typed hold via the conditional-DML path
    assert store._database.run_in_transaction(lambda t: reserve_credit(t, pt, ws, 300_000))
    assert _typed_credit(db, ws)["reserved"] == 300_000

    # a JSON credit top-up (mirror fires) must NOT clobber the in-flight hold
    store.credit_workspace_once(ws, 500_000, "evt_topup")
    assert _typed_credit(db, ws)["total_credits"] == 1_500_000  # credit applied
    assert _typed_credit(db, ws)["reserved"] == 300_000  # hold preserved

    # the typed release still finds its hold: row-count == 1, NOT the incident's 0
    assert store._database.run_in_transaction(
        lambda t: release_credit(t, pt, ws, 300_000, 290_000)
    ) == 1
    assert _typed_credit(db, ws)["reserved"] == 0
    assert _typed_credit(db, ws)["total_usage"] == 290_000


def test_api_key_config_is_mirrored_usage_is_not() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_key_mirror"
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    assert_key_config_mirrored(db, key.hash)
    # A legacy JSON usage write updates JSON but not the typed-owned usage.
    store.api_keys.add_usage(key.hash, 120_000, is_byok=False)
    assert _json_key(db, key.hash)["usage_microdollars"] == 120_000
    assert_key_config_mirrored(db, key.hash)
    assert _typed_key(db, key.hash)["usage"] == 0


def test_json_key_write_does_not_clobber_typed_key_hold() -> None:
    """Key-side analogue of the incident: a typed key hold must survive a JSON
    api_key write (here a usage write, which also fires the mirror)."""
    store, db, _ = make_fake_store()
    ws = "ws_key_clobber"
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=2_000_000
    )
    pt = store._param_types
    store._database.run_in_transaction(
        lambda t: reserve_key(t, pt, key.hash, 600_000, is_byok=False)
    )
    assert _typed_key(db, key.hash)["reserved"] == 600_000
    # a JSON api_key write fires the mirror; the typed hold must be untouched
    store.api_keys.add_usage(key.hash, 100_000, is_byok=False)
    assert _typed_key(db, key.hash)["reserved"] == 600_000  # hold preserved
    assert _typed_key(db, key.hash)["limit_micro"] == 2_000_000  # config still mirrored


def test_api_key_delete_removes_typed_mirror_row() -> None:
    """Deleting the authoritative JSON api_key must also drop the typed mirror,
    or Step 2 reconciliation sees a phantom typed row (drift)."""
    store, db, _ = make_fake_store()
    ws = "ws_delete"
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    assert (key.hash, 0) in db.typed[KEY_LIMIT_TABLE]

    assert store.api_keys.delete(key.hash) is True
    assert ("api_key", key.hash) not in db.rows
    assert (key.hash, 0) not in db.typed.get(KEY_LIMIT_TABLE, {})


def test_mirror_disabled_writes_no_typed_rows() -> None:
    """Default-off safety: with the flag off, no typed rows are written, so the
    code is safe to deploy before the DDL exists."""
    store, db, _ = make_fake_store()
    store._counter_mirror_enabled = False
    ws = "ws_flag_off"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    store.reserve(ws, "key_1", 100_000)
    assert CREDIT_BALANCE_TABLE not in db.typed or (ws, 0) not in db.typed.get(
        CREDIT_BALANCE_TABLE, {}
    )
    # JSON path unaffected.
    assert _json_credit(db, ws)["reserved_microdollars"] == 100_000


def test_uncapped_key_mirrors_null_limit() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_uncapped"
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=None
    )
    assert _typed_key(db, key.hash)["limit_micro"] is None
    assert_key_config_mirrored(db, key.hash)


# ── Step 2: backfill + drift comparator ─────────────────────────────────────

def test_credit_drift_pure() -> None:
    body = {
        "total_credits_microdollars": 1_000_000,
        "total_usage_microdollars": 200_000,
        "reserved_microdollars": 50_000,
    }
    match = {"total_credits": 1_000_000, "total_usage": 200_000, "reserved": 50_000}
    assert credit_drift(body, match) == {}
    assert credit_drift(body, None)  # missing mirror = drift on the owned field
    # reserved + total_usage are typed-owned now: a mismatch there is NOT drift.
    assert credit_drift(body, dict(match, reserved=49_999)) == {}
    assert credit_drift(body, dict(match, total_usage=199_999)) == {}
    # but a JSON-owned total_credits mismatch IS drift.
    assert credit_drift(body, dict(match, total_credits=999_999)) == {
        "total_credits": (1_000_000, 999_999)
    }


def test_key_drift_pure_handles_null_limit_and_bool() -> None:
    body = {
        "limit_microdollars": None,
        "usage_microdollars": 10,
        "byok_usage_microdollars": 0,
        "reserved_microdollars": 0,
        "include_byok_in_limit": True,
    }
    typed = {"limit_micro": None, "usage": 10, "byok_usage": 0, "reserved": 0, "include_byok": True}
    assert key_drift(body, typed) == {}
    assert "include_byok" in key_drift(body, dict(typed, include_byok=False))
    # typed-owned usage mismatch is NOT drift.
    assert key_drift(body, dict(typed, usage=999)) == {}


def test_compare_clean_after_mirror() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_cmp"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=3_000_000)
    )
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=2_000_000
    )
    store.reserve(ws, key.hash, 400_000)
    store.reserve_key_limit(key.hash, 400_000, usage_type="Credits")

    report = compare(store)
    assert report.clean, report.summary() + f" {report.samples}"
    assert report.credit_rows == 1
    assert report.key_rows == 1


def test_compare_ignores_typed_owned_columns_flags_json_owned() -> None:
    """The comparator only audits JSON-owned columns. Corrupting the typed-owned
    reserved is expected divergence (typed owns it); corrupting the JSON-owned
    total_credits is real drift."""
    store, db, _ = make_fake_store()
    ws = "ws_corrupt"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    assert compare(store).clean
    # Typed-owned reserved diverging is NOT drift.
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["reserved"] = 999_999
    assert compare(store).clean
    # JSON-owned total_credits diverging IS drift.
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_credits"] = 12_345
    report = compare(store)
    assert not report.clean
    assert report.credit_drift == 1
    assert f"credit:{ws}" in report.samples


def test_backfill_fills_pre_flag_rows() -> None:
    """Rows written before the flag was on have no typed mirror; backfill adds
    them and compare goes clean."""
    store, db, _ = make_fake_store()
    store._counter_mirror_enabled = False  # simulate pre-flag writes (JSON only)
    ws = "ws_backfill"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=2_500_000)
    )
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, limit_microdollars=1_000_000
    )
    # No typed rows yet -> compare sees missing-mirror drift.
    assert CREDIT_BALANCE_TABLE not in db.typed
    assert not compare(store).clean

    counts = backfill(store)
    assert counts == {"credit": 1, "api_key": 1}
    report = compare(store)
    assert report.clean, report.summary() + f" {report.samples}"


def test_backfill_is_idempotent() -> None:
    store, _db, _ = make_fake_store()
    store._counter_mirror_enabled = False
    ws = "ws_idem"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    backfill(store)
    backfill(store)  # second run must not corrupt anything
    assert compare(store).clean


def test_compare_detects_orphan_typed_row() -> None:
    """A typed row with no JSON authority (e.g. missed delete) must be flagged,
    not silently CLEAN (codex Step-2 #1)."""
    store, db, _ = make_fake_store()
    ws = "ws_orphan"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    assert compare(store).clean
    # Authoritative JSON row vanishes but the typed mirror lingers.
    del db.rows[("credit", ws)]
    report = compare(store)
    assert not report.clean
    assert report.credit_orphans == 1
    assert f"credit-orphan:{ws}" in report.samples


def test_drift_no_false_positive_on_omitted_legacy_fields() -> None:
    """Legacy JSON bodies that omit limit_microdollars / include_byok_in_limit
    must not read as drift against a correctly-defaulted typed row."""
    # Uncapped, include_byok defaulted true, counters absent.
    legacy_key = {"hash": "k", "usage_microdollars": 0}
    typed = {"limit_micro": None, "usage": 0, "byok_usage": 0, "reserved": 0, "include_byok": True}
    assert key_drift(legacy_key, typed) == {}
    # bool vs int representation of include_byok must compare equal.
    assert key_drift(legacy_key, dict(typed, include_byok=1)) == {}


def test_backfill_dry_run_still_compares_and_signals_drift() -> None:
    """--dry-run must not look like a clean gate: compare still runs (codex #4)."""
    store, db, _ = make_fake_store()
    store._counter_mirror_enabled = False
    ws = "ws_dry"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    # dry-run plans the row but writes nothing -> drift remains.
    counts = backfill(store, dry_run=True)
    assert counts["credit"] == 1
    assert CREDIT_BALANCE_TABLE not in db.typed
    assert not compare(store).clean
