from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from scripts import typed_flip
from tests.fakes.spanner import make_fake_store
from trusted_router.storage import CreditAccount, Workspace
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


@pytest.fixture(autouse=True)
def _typed_flip_apply_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")


def _seed_workspace(
    store: Any,
    ws: str,
    *,
    paused: bool = True,
    total_credits: int = 5_000_000,
    total_usage: int = 0,
    reserved: int = 0,
) -> None:
    store._write_entity(
        "workspace",
        ws,
        Workspace(id=ws, name="test", owner_user_id="u", billing_paused=paused),
    )
    store._write_entity(
        "credit",
        ws,
        CreditAccount(
            workspace_id=ws,
            total_credits_microdollars=total_credits,
            total_usage_microdollars=total_usage,
            reserved_microdollars=reserved,
        ),
    )


def _typed_credit(db: Any, ws: str) -> dict:
    return db.typed[CREDIT_BALANCE_TABLE][(ws, 0)]


def _snapshot_state(db: Any) -> tuple[dict, dict, dict]:
    return (deepcopy(db.rows), deepcopy(db.typed), deepcopy(db.reservations))


def test_readiness_verdicts_ready_not_ready_and_already_typed() -> None:
    store, db, _ = make_fake_store()

    _seed_workspace(store, "ws_ready", paused=True)
    assert typed_flip.assess_readiness(store, "ws_ready").verdict == "READY"

    _seed_workspace(store, "ws_reserved", paused=True)
    store.reserve("ws_reserved", "key", 100_000)
    reserved = typed_flip.assess_readiness(store, "ws_reserved")
    assert reserved.verdict == "NOT_READY"
    assert any("reserved" in reason for reason in reserved.reasons)
    assert reserved.legacy_open_reservations == 1

    _seed_workspace(store, "ws_drift", paused=True)
    _typed_credit(db, "ws_drift")["total_credits"] = 123
    drift = typed_flip.assess_readiness(store, "ws_drift")
    assert drift.verdict == "NOT_READY"
    assert any("compare() drift" in reason for reason in drift.reasons)

    _seed_workspace(store, "ws_typed", paused=True)
    db.reservations["r_hist"] = {
        "reservation_id": "r_hist",
        "workspace_id": "ws_typed",
        "settled": True,
    }
    assert typed_flip.assess_readiness(store, "ws_typed").verdict == "ALREADY_TYPED"

    db.reservations["r_open"] = {
        "reservation_id": "r_open",
        "workspace_id": "ws_typed",
        "credit_reserved_micro": 1,
        "settled": False,
    }
    open_typed = typed_flip.assess_readiness(store, "ws_typed")
    assert open_typed.verdict == "NOT_READY"
    assert any("open tr_reservation" in reason for reason in open_typed.reasons)


def test_prepare_apply_pauses_seeds_verifies_and_leaves_paused(capsys: pytest.CaptureFixture[str]) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_prepare"
    _seed_workspace(store, ws, paused=False, total_credits=5_000_000, total_usage=1_200_000)

    assert _typed_credit(db, ws)["total_usage"] == 0
    rc = typed_flip.main(["prepare", "--workspace", ws, "--apply"], store=store)

    assert rc == 0
    assert store.get_workspace(ws).billing_paused is True
    assert _typed_credit(db, ws)["total_credits"] == 5_000_000
    assert _typed_credit(db, ws)["total_usage"] == 1_200_000
    assert _typed_credit(db, ws)["reserved"] == 0
    out = capsys.readouterr().out
    assert "NEXT STEPS:" in out
    assert "Workspace REMAINS PAUSED." in out


def test_prepare_apply_pauses_and_parks_on_existing_legacy_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_prepare_hold"
    _seed_workspace(store, ws, paused=False)
    store.reserve(ws, "key", 100_000)
    typed_before = deepcopy(db.typed)

    rc = typed_flip.main(["prepare", "--workspace", ws, "--apply"], store=store)

    assert rc == 2
    assert store.get_workspace(ws).billing_paused is True
    assert db.typed == typed_before
    out = capsys.readouterr().out
    assert "re-run prepare after in-flight requests settle" in out
    assert "drain-blocked: JSON credit reserved=100000" in out
    assert "drain-blocked: 1 open legacy reservations" in out


def test_prepare_apply_refuses_drift_before_pausing() -> None:
    store, db, _ = make_fake_store()
    ws_drift = "ws_prepare_drift"
    _seed_workspace(store, ws_drift, paused=False)
    _typed_credit(db, ws_drift)["total_credits"] = 99

    assert typed_flip.main(["prepare", "--workspace", ws_drift, "--apply"], store=store) == 1
    assert store.get_workspace(ws_drift).billing_paused is False


def test_prepare_apply_refuses_already_typed_before_pausing() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_prepare_already_typed"
    _seed_workspace(store, ws, paused=False)
    db.reservations["r_hist"] = {
        "reservation_id": "r_hist",
        "workspace_id": ws,
        "settled": True,
    }

    assert typed_flip.main(["prepare", "--workspace", ws, "--apply"], store=store) == 1
    assert store.get_workspace(ws).billing_paused is False


def test_prepare_apply_exits_2_when_hold_appears_after_pause(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_prepare_race"
    _seed_workspace(store, ws, paused=False, total_credits=5_000_000, total_usage=750_000)
    original_update = store.update_workspace

    def update_workspace_with_racing_hold(*args: Any, **kwargs: Any) -> Any:
        result = original_update(*args, **kwargs)
        if kwargs.get("billing_paused") is True:
            store.reserve(ws, "key", 100_000)
        return result

    monkeypatch.setattr(store, "update_workspace", update_workspace_with_racing_hold)

    rc = typed_flip.main(["prepare", "--workspace", ws, "--apply"], store=store)

    assert rc == 2
    assert store.get_workspace(ws).billing_paused is True
    assert _typed_credit(db, ws)["total_usage"] == 0
    assert "re-run prepare after in-flight requests settle" in capsys.readouterr().out


def test_prepare_dry_run_with_existing_legacy_hold_would_pause_and_park(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_prepare_dry_hold"
    _seed_workspace(store, ws, paused=False)
    store.reserve(ws, "key", 100_000)
    before = _snapshot_state(db)

    rc = typed_flip.main(["prepare", "--workspace", ws], store=store)

    assert rc == 0
    assert _snapshot_state(db) == before
    out = capsys.readouterr().out
    assert "DRY-RUN: would set billing_paused=True" in out
    assert "DRY-RUN: would park paused until in-flight requests settle" in out
    assert "DRY-RUN: would refuse before pausing" not in out
    assert "DRY-RUN: would run reconcile_for_flip(..., apply=True)" not in out


def test_finish_refuses_without_attestation_and_apply_unpauses() -> None:
    store, _db, _ = make_fake_store()
    ws = "ws_finish"
    _seed_workspace(store, ws, paused=True)

    assert typed_flip.main(["finish", "--workspace", ws, "--apply"], store=store) == 1
    assert store.get_workspace(ws).billing_paused is True

    rc = typed_flip.main(
        ["finish", "--workspace", ws, "--allowlist-deployed", "--apply"],
        store=store,
    )

    assert rc == 0
    assert store.get_workspace(ws).billing_paused is False


def test_dry_runs_do_not_mutate_store_state() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_dry"
    _seed_workspace(store, ws, paused=False, total_credits=5_000_000, total_usage=900_000)
    before_prepare = _snapshot_state(db)

    assert typed_flip.main(["prepare", "--workspace", ws], store=store) == 0
    assert _snapshot_state(db) == before_prepare

    store.update_workspace(ws, billing_paused=True, billing_pause_reason="test")
    _typed_credit(db, ws)["total_usage"] = 900_000
    before_finish = _snapshot_state(db)

    assert typed_flip.main(["finish", "--workspace", ws, "--allowlist-deployed"], store=store) == 0
    assert _snapshot_state(db) == before_finish

    store.update_workspace(ws, billing_paused=False, billing_pause_reason="")
    before_rollback = _snapshot_state(db)

    assert typed_flip.main(["rollback", "--workspace", ws], store=store) == 0
    assert _snapshot_state(db) == before_rollback
