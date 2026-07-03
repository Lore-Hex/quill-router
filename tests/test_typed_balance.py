"""Typed-aware balance reads: for a typed workspace, display + auto-refill must
read the authoritative typed counters (usage/reserved booked by the typed DML),
not the intentionally-stale JSON. For a legacy workspace they read JSON.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.typed_balance import typed_aware_credit_account


def _settings(allow: str = "", deny: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        typed_billing_workspace_ids=allow, typed_billing_workspace_denylist=deny
    )


def test_credit_overlay_only_for_typed_workspace() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_typed"
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000, total_usage_microdollars=0),
    )
    # Simulate the typed DML having booked usage + a hold ahead of stale JSON.
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 300_000, "reserved": 150_000})

    # Legacy view (not in allowlist) → JSON (usage/reserved still 0).
    legacy = typed_aware_credit_account(store, ws, settings=_settings(allow=""))
    assert legacy.total_usage_microdollars == 0
    assert legacy.reserved_microdollars == 0

    # Typed view (in allowlist) → authoritative typed counters.
    typed = typed_aware_credit_account(store, ws, settings=_settings(allow=ws))
    assert typed.total_credits_microdollars == 1_000_000  # JSON-owned, unchanged
    assert typed.total_usage_microdollars == 300_000
    assert typed.reserved_microdollars == 150_000
    assert typed.workspace_id == ws  # JSON metadata preserved


def test_credit_overlay_denylist_wins() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_deny"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 999_999})
    # "*" allowlist but denied → legacy (JSON, usage 0).
    acct = typed_aware_credit_account(store, ws, settings=_settings(allow="*", deny=ws))
    assert acct.total_usage_microdollars == 0


def test_credit_overlay_denylist_wildcard_global_kill_reads_stale_json() -> None:
    """The "*" global kill-switch routes the balance read to JSON for EVERY
    workspace — and this test pins the documented hazard: a workspace that was
    running typed has stale-LOW JSON usage, so under the kill it under-reports
    spend (999_999 booked in typed, but JSON reads 0). That is exactly why the
    kill-switch is a break-glass availability brake, not a billing-clean
    rollback (the pause->drain->backsync runbook reconciles JSON first)."""
    store, db, _ = make_fake_store()
    ws = "ws_globalkill"
    store._write_entity(
        "credit", ws, CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000)
    )
    db.typed[CREDIT_BALANCE_TABLE][(ws, 0)].update({"total_usage": 999_999})
    # "*" allowlist (everyone-on cutover) + "*" denylist (global kill) → JSON.
    acct = typed_aware_credit_account(store, ws, settings=_settings(allow="*", deny="*"))
    assert acct.total_usage_microdollars == 0  # stale-low JSON, NOT the typed 999_999


def test_credit_typed_but_unseeded_falls_back_to_json() -> None:
    store, _db, _ = make_fake_store()
    store._counter_mirror_enabled = False  # no typed row written
    ws = "ws_unseeded"
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=2_000_000, total_usage_microdollars=50_000),
    )
    acct = typed_aware_credit_account(store, ws, settings=_settings(allow=ws))
    assert acct is not None
    assert acct.total_usage_microdollars == 50_000  # JSON, since no typed row


def test_credit_missing_account_returns_none() -> None:
    store, _db, _ = make_fake_store()
    assert typed_aware_credit_account(store, "nope", settings=_settings(allow="*")) is None
