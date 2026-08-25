"""Drift detection and repair for typed `total_usage`.

The module already audits and repairs typed `reserved`. Usage had neither, and
the retired PR #89 proposed the invariant

    total_usage == JSON baseline + Σ settled-Credits actual_micro

which is no longer the whole story on this store. `total_usage` now has TWO
writers: the typed settle, and deferred federated settlement applying a peer
plane's debt with no reservation in this plane at all. Summing only the first
reads every federated microdollar as drift, so the federated case has a test of
its own below -- it is the one #89's arithmetic would have got wrong.

The second thing these pin is what happens when the baseline is GONE. It is a
residual raw key in the credit JSON body, and the reviewed cleanup migration
deletes it without archiving the value, so an absent baseline is ambiguous: a
post-flip workspace whose real baseline is 0, or a pre-flip workspace whose
baseline was removed. Reporting it as drift would cry wolf on every cleaned
workspace; reporting it as clean would overstate what was checked. It is
counted separately, and `fully_audited` is how an operator sees the difference.
"""

from __future__ import annotations

import json
from typing import Any

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import Workspace
from trusted_router.storage_gcp_counter_reconcile import (
    audit_typed_invariants,
    repair_typed_usage,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

_CLAIM_KIND = "federated_settlement_claim"


def _workspace(store: Any, ws: str, *, paused: bool = True) -> None:
    store._write_entity(
        "workspace", ws, Workspace(id=ws, name="t", owner_user_id="u", billing_paused=paused)
    )


def _credit(store: Any, db: Any, ws: str, *, baseline: int | None, total_usage: int) -> None:
    body: dict[str, Any] = {"workspace_id": ws, "shard_count": 1}
    if baseline is not None:
        body["total_usage_microdollars"] = baseline
    store._write_entity("credit", ws, body)
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws,
        "shard": 0,
        "total_credits": 100_000_000,
        "total_usage": total_usage,
        "reserved": 0,
    }


def _settled(db: Any, rid: str, ws: str, actual: int, *, usage_type: str = "Credits") -> None:
    db.reservations[rid] = {
        "reservation_id": rid,
        "workspace_id": ws,
        "ws_shard": 0,
        "settled": True,
        "settled_usage_type": usage_type,
        "actual_micro": actual,
        "credit_reserved_micro": actual,
    }


def _open_hold(db: Any, rid: str, ws: str, held: int) -> None:
    db.reservations[rid] = {
        "reservation_id": rid,
        "workspace_id": ws,
        "ws_shard": 0,
        "settled": False,
        "credit_reserved_micro": held,
    }


def _federated_claim(store: Any, ws: str, claim_id: str, cost: int) -> None:
    store._write_entity(
        _CLAIM_KIND,
        claim_id,
        json.loads(json.dumps({
            "source_plane": "aws-eu",
            "authorization_id": f"auth_{claim_id}",
            "workspace_id": ws,
            "cost_microdollars": cost,
        })),
    )


def test_usage_matching_the_ledger_is_clean() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_ok"
    _credit(store, db, ws, baseline=2_000_000, total_usage=2_450_000)
    _settled(db, "r1", ws, 300_000)
    _settled(db, "r2", ws, 150_000)

    report = audit_typed_invariants(store)

    assert report.usage_rows == 1
    assert report.usage_violations == 0
    assert report.usage_unauditable == 0
    assert report.clean and report.fully_audited


def test_federated_settlement_is_booked_usage_not_drift() -> None:
    """The case PR #89's arithmetic would have failed.

    A peer plane served this workspace's traffic and delivered the debt. The
    home plane added it straight to total_usage and there is NO reservation
    here, because the authorize happened on the other plane. An audit that sums
    only settled reservations reports the whole federated amount as drift.
    """
    store, db, _ = make_fake_store()
    ws = "ws_federated"
    _credit(store, db, ws, baseline=1_000_000, total_usage=1_875_000)
    _settled(db, "r1", ws, 125_000)
    _federated_claim(store, ws, "c1", 500_000)
    _federated_claim(store, ws, "c2", 250_000)

    report = audit_typed_invariants(store)

    assert report.usage_violations == 0, report.samples
    assert report.clean

    # And the arithmetic really did depend on those claims: drop them and the
    # same counter becomes a violation of exactly their size.
    for claim_id in ("c1", "c2"):
        del db.rows[(_CLAIM_KIND, claim_id)]
    regressed = audit_typed_invariants(store)
    assert regressed.usage_violations == 1
    assert regressed.samples[f"usage:{ws}"]["delta"] == 750_000


def test_non_credits_settles_do_not_count_toward_usage() -> None:
    """storage_gcp_authorize passes `book_actual if settled_usage_type ==
    "Credits" else 0`, so a non-Credits settle never reached total_usage."""
    store, db, _ = make_fake_store()
    ws = "ws_mixed"
    _credit(store, db, ws, baseline=0, total_usage=100_000)
    _settled(db, "r1", ws, 100_000)
    _settled(db, "r2", ws, 900_000, usage_type="Subscription")

    report = audit_typed_invariants(store)

    assert report.usage_violations == 0, report.samples


def test_real_usage_drift_is_caught_with_its_arithmetic() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_drift"
    _credit(store, db, ws, baseline=2_000_000, total_usage=2_000_000)  # settle never landed
    _settled(db, "r1", ws, 420_000)

    report = audit_typed_invariants(store)

    assert report.usage_violations == 1
    assert not report.clean
    sample = report.samples[f"usage:{ws}"]
    assert sample["expected"] == 2_420_000
    assert sample["typed_total_usage"] == 2_000_000
    assert sample["delta"] == -420_000
    assert sample["settled_actuals"] == 420_000


def test_a_missing_baseline_is_unauditable_not_a_violation() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_cleaned"
    _credit(store, db, ws, baseline=None, total_usage=9_999_999)
    _settled(db, "r1", ws, 1_000)

    report = audit_typed_invariants(store)

    assert report.usage_violations == 0
    assert report.usage_unauditable == 1
    # CLEAN, but explicitly NOT fully audited -- the distinction an operator needs.
    assert report.clean
    assert not report.fully_audited
    assert "(PARTIAL)" in report.summary()

    # Reported OUT of `samples`. Callers label that dict wholesale --
    # scripts/audit_typed_counters.py passes one `sample_label` for all of it --
    # so an unauditable row left in there prints as "VIOLATION" underneath a
    # summary that says CLEAN. Production's first run printed 643 such lines.
    assert report.unauditable[f"usage-unauditable:{ws}"]["baseline"] is None
    assert not report.samples, "unauditable rows must not sit in the violation samples"


def test_booked_usage_with_no_typed_row_is_a_violation() -> None:
    """The reverse direction, matching the reserved arm: a booking that landed
    nowhere is invisible if you only iterate typed rows."""
    store, db, _ = make_fake_store()
    ws = "ws_orphan"
    _settled(db, "r1", ws, 640_000)

    report = audit_typed_invariants(store)

    assert report.usage_violations == 1
    assert report.samples[f"usage-orphan-booking:{ws}"]["settled_actuals"] == 640_000


def test_repair_dry_run_reports_without_writing() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_repair"
    _workspace(store, ws)
    _credit(store, db, ws, baseline=2_000_000, total_usage=2_000_000)
    _settled(db, "r1", ws, 420_000)
    _federated_claim(store, ws, "c1", 80_000)

    result = repair_typed_usage(store, ws, apply=False)

    assert result.ready and not result.applied
    assert result.total_usage_before == 2_000_000
    assert result.total_usage_after == 2_500_000
    assert result.baseline == 2_000_000
    assert result.settled_actuals == 420_000
    assert result.federated_applied == 80_000
    assert result.delta == 500_000
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 2_000_000


def test_repair_applies_the_reconstructed_total() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_apply"
    _workspace(store, ws)
    _credit(store, db, ws, baseline=2_000_000, total_usage=2_000_000)
    _settled(db, "r1", ws, 420_000)

    result = repair_typed_usage(store, ws, apply=True)

    assert result.applied
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 2_420_000
    assert audit_typed_invariants(store).usage_violations == 0


def test_repair_refuses_an_unpaused_workspace() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_live"
    _workspace(store, ws, paused=False)
    _credit(store, db, ws, baseline=0, total_usage=0)

    result = repair_typed_usage(store, ws, apply=True)

    assert not result.ready and not result.applied
    assert any("billing-paused" in r for r in result.reasons)


def test_repair_refuses_while_a_hold_is_open() -> None:
    """An open hold is a settle that has not added its actual yet: repairing now
    writes a total that the settle immediately invalidates."""
    store, db, _ = make_fake_store()
    ws = "ws_draining"
    _workspace(store, ws)
    _credit(store, db, ws, baseline=1_000_000, total_usage=1_000_000)
    _open_hold(db, "r_open", ws, 50_000)

    result = repair_typed_usage(store, ws, apply=True)

    assert not result.ready and not result.applied
    assert any("holds still open" in r for r in result.reasons)


def test_repair_refuses_when_the_baseline_was_cleaned_up() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_nobaseline"
    _workspace(store, ws)
    _credit(store, db, ws, baseline=None, total_usage=5_000_000)

    result = repair_typed_usage(store, ws, apply=True)

    assert not result.ready and not result.applied
    assert any("baseline" in r for r in result.reasons)


def test_repair_refuses_to_lower_usage_unless_told_to() -> None:
    """total_usage is monotonic, so a computed decrease means the ledger is
    missing rows -- not that the counter is too high."""
    store, db, _ = make_fake_store()
    ws = "ws_down"
    _workspace(store, ws)
    _credit(store, db, ws, baseline=1_000_000, total_usage=5_000_000)

    refused = repair_typed_usage(store, ws, apply=True)
    assert not refused.ready and not refused.applied
    assert any("refusing to lower" in r for r in refused.reasons)
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 5_000_000

    forced = repair_typed_usage(store, ws, apply=True, allow_decrease=True)
    assert forced.applied
    assert db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]["total_usage"] == 1_000_000


def test_usage_is_summed_across_shards() -> None:
    """Unlike `reserved`, which is per (scope, shard), both usage ledgers name a
    WORKSPACE. Comparing shard 0 alone against a whole-workspace ledger would
    report every other shard's usage as missing."""
    store, db, _ = make_fake_store()
    ws = "ws_sharded"
    # baseline 1M + settled 1M == 2M, spread 800k/700k/500k across three shards.
    _credit(store, db, ws, baseline=1_000_000, total_usage=800_000)  # shard 0
    db.typed[CREDIT_BALANCE_TABLE][(ws, 1)] = {
        "workspace_id": ws, "shard": 1, "total_credits": 0,
        "total_usage": 700_000, "reserved": 0,
    }
    db.typed[CREDIT_BALANCE_TABLE][(ws, 2)] = {
        "workspace_id": ws, "shard": 2, "total_credits": 0,
        "total_usage": 500_000, "reserved": 0,
    }
    _settled(db, "r1", ws, 1_000_000)

    report = audit_typed_invariants(store)

    assert report.usage_rows == 1, "one workspace, not one row per shard"
    assert report.usage_violations == 0, report.samples
