"""The operator path that records a pre-ledger usage baseline."""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts import record_usage_baselines
from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counter_reconcile import (
    USAGE_BASELINE_KIND,
    audit_typed_invariants,
    propose_usage_baselines,
    record_usage_baseline,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


def _seed(store: Any, db: Any, ws: str, *, usage: int, settled: int) -> None:
    store._write_entity("credit", ws, {"workspace_id": ws, "shard_count": 1})
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": 0,
        "total_usage": usage, "reserved": 0,
    }
    if settled:
        db.reservations[f"r_{ws}"] = {
            "reservation_id": f"r_{ws}", "workspace_id": ws, "ws_shard": 0,
            "settled": True, "settled_usage_type": "Credits",
            "actual_micro": settled, "credit_reserved_micro": settled,
        }


def test_only_counters_exceeding_their_ledger_are_proposed() -> None:
    store, db, _ = make_fake_store()
    _seed(store, db, "ws_explained", usage=500_000, settled=500_000)   # baseline 0
    _seed(store, db, "ws_history", usage=5_000_000, settled=1_000_000)  # 4M of history

    proposals = {p.workspace_id: p for p in propose_usage_baselines(store)}

    assert "ws_explained" not in proposals, "a zero baseline needs no row"
    assert proposals["ws_history"].baseline_microdollars == 4_000_000


def test_recording_makes_the_workspace_auditable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, db, _ = make_fake_store()
    _seed(store, db, "ws_history", usage=5_000_000, settled=1_000_000)
    assert audit_typed_invariants(store).usage_unauditable == 1

    assert record_usage_baselines.main(["--apply"], store=store) == 0

    report = audit_typed_invariants(store)
    assert report.usage_unauditable == 0
    assert report.usage_violations == 0
    assert report.clean and report.fully_audited
    body = json.loads(db.rows[(USAGE_BASELINE_KIND, "ws_history")].body)
    assert body["baseline_microdollars"] == 4_000_000
    # The evidence it was derived from, not just the number.
    assert body["typed_total_usage_at_record"] == 5_000_000
    assert body["ledger_booked_at_record"] == 1_000_000


def test_dry_run_writes_nothing() -> None:
    store, db, _ = make_fake_store()
    _seed(store, db, "ws_history", usage=5_000_000, settled=1_000_000)

    assert record_usage_baselines.main([], store=store) == 0

    assert (USAGE_BASELINE_KIND, "ws_history") not in db.rows
    assert audit_typed_invariants(store).usage_unauditable == 1


def test_an_existing_baseline_is_never_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second, different value for one workspace means one is wrong."""
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, db, _ = make_fake_store()
    _seed(store, db, "ws_history", usage=5_000_000, settled=1_000_000)
    store._write_entity(
        USAGE_BASELINE_KIND,
        "ws_history",
        {"workspace_id": "ws_history", "baseline_microdollars": 123},
    )

    assert record_usage_baselines.main(["--apply"], store=store) == 0

    body = json.loads(db.rows[(USAGE_BASELINE_KIND, "ws_history")].body)
    assert body["baseline_microdollars"] == 123


def test_a_baseline_recorded_between_propose_and_write_is_not_clobbered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the in-transaction check exists for.

    The early return covers the case where the proposal already knows about a
    recorded baseline. It does NOT cover a baseline that appears AFTER the
    proposal was built, which is the only way two writers collide -- and a
    mutation that deleted the in-transaction guard left every other test in this
    file green.
    """
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, db, _ = make_fake_store()
    _seed(store, db, "ws_race", usage=5_000_000, settled=1_000_000)

    # Built while nothing was recorded, exactly as the CLI builds it.
    proposal = next(
        p for p in propose_usage_baselines(store) if p.workspace_id == "ws_race"
    )
    assert proposal.already_recorded is None
    assert proposal.baseline_microdollars == 4_000_000

    # Someone else records one first.
    store._write_entity(
        USAGE_BASELINE_KIND,
        "ws_race",
        {"workspace_id": "ws_race", "baseline_microdollars": 777},
    )

    wrote = record_usage_baseline(
        store, proposal, recorded_at="2026-08-25T00:00:00Z", apply=True
    )

    assert wrote is False
    body = json.loads(db.rows[(USAGE_BASELINE_KIND, "ws_race")].body)
    assert body["baseline_microdollars"] == 777, "the other writer's value stands"
