from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from scripts.backfill_reservation_terminal_at import run_backfill
from tests.fakes.spanner import make_fake_store


def _seed_reservation(
    database: Any,
    reservation_id: str,
    *,
    settled: bool = True,
    terminal_at: datetime | None = None,
) -> None:
    database.reservations[reservation_id] = {
        "reservation_id": reservation_id,
        "authorization_id": f"auth-{reservation_id}",
        "settled": settled,
        "terminal_at": terminal_at,
    }


def _freeze(database: Any, reservation_id: str, status: str) -> None:
    authorization_id = f"auth-{reservation_id}"
    database.settle_outbox[(authorization_id, "settle")] = {
        "authorization_id": authorization_id,
        "intent_kind": "settle",
        "status": status,
    }


def test_apply_arms_settled_null_rows_and_terminates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, database, _bigtable = make_fake_store()
    for index in range(3):
        _seed_reservation(database, f"res-{index}")

    assert run_backfill(store, apply=True) == 0

    assert all(
        row["terminal_at"] is not None
        for row in database.reservations.values()
    )
    output = capsys.readouterr().out
    assert output.startswith(
        "STATUS: tr_reservation candidates=3 armed=0 open_holds=0 excluded=0\n"
    )
    assert "batch 1: updated=3 running_total=3" in output
    assert "FINAL: armed=3 remaining_candidates=0 excluded=0" in output
    assert output.endswith("COMPLETE: no unarmed settled reservations remain\n")


@pytest.mark.parametrize("outbox_status", ["pending", "dead"])
def test_apply_excludes_frozen_intent_rows_and_stops(
    outbox_status: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, database, _bigtable = make_fake_store()
    for index in range(3):
        _seed_reservation(database, f"res-{index}")
    _freeze(database, "res-1", outbox_status)

    assert run_backfill(store, batch=1, apply=True) == 0

    assert database.reservations["res-0"]["terminal_at"] is not None
    assert database.reservations["res-1"]["terminal_at"] is None
    assert database.reservations["res-2"]["terminal_at"] is not None
    output = capsys.readouterr().out
    assert "candidates=3 armed=0 open_holds=0 excluded=1" in output
    assert "FINAL: armed=2 remaining_candidates=1 excluded=1" in output
    assert output.endswith(
        "STOP: all remaining candidates are excluded by the pending/dead "
        "tr_settle_outbox frozen-intent guard\n"
    )


def test_apply_never_touches_open_holds() -> None:
    store, database, _bigtable = make_fake_store()
    _seed_reservation(database, "settled")
    _seed_reservation(database, "open", settled=False)

    assert run_backfill(store, apply=True) == 0

    assert database.reservations["settled"]["terminal_at"] is not None
    assert database.reservations["open"]["terminal_at"] is None


def test_apply_preserves_already_armed_rows() -> None:
    store, database, _bigtable = make_fake_store()
    existing = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    _seed_reservation(database, "already-armed", terminal_at=existing)
    _seed_reservation(database, "candidate")

    assert run_backfill(store, apply=True) == 0

    assert database.reservations["already-armed"]["terminal_at"] == existing
    assert database.reservations["candidate"]["terminal_at"] is not None


def test_dry_run_mutates_nothing_and_reports_candidates_and_exclusions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, database, _bigtable = make_fake_store()
    _seed_reservation(database, "eligible")
    _seed_reservation(database, "frozen")
    _seed_reservation(database, "open", settled=False)
    _seed_reservation(
        database,
        "armed",
        terminal_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _freeze(database, "frozen", "pending")
    database.gateway_authorizations["gateway-pinned"] = {
        "authorization_id": "gateway-pinned",
        "settled": True,
        "terminal_at": None,
    }
    before = deepcopy(database.reservations)

    assert run_backfill(store, batch=7) == 0

    assert database.reservations == before
    output = capsys.readouterr().out
    assert output.startswith(
        "STATUS: tr_reservation candidates=2 armed=1 open_holds=1 excluded=1\n"
    )
    assert (
        "STATUS: tr_gateway_authorization pinned=1 "
        "(informational cross-check)\n"
    ) in output
    assert output.endswith("DRY-RUN: would arm 1 rows in batches of 7\n")


def test_apply_processes_more_candidates_than_batch_in_multiple_batches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, database, _bigtable = make_fake_store()
    for index in range(5):
        _seed_reservation(database, f"res-{index}")

    assert run_backfill(store, batch=2, apply=True) == 0

    assert all(
        row["terminal_at"] is not None
        for row in database.reservations.values()
    )
    output = capsys.readouterr().out
    assert "batch 1: updated=2 running_total=2" in output
    assert "batch 2: updated=2 running_total=4" in output
    assert "batch 3: updated=1 running_total=5" in output


def test_race_row_becomes_eligible_after_empty_select_is_still_armed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A guarded row whose intent flips to release_approved between the empty
    select and the status read must be re-selected and armed, not abandoned
    behind a false "all excluded" STOP (review finding on #357)."""
    import scripts.backfill_reservation_terminal_at as mod

    store, database, _bigtable = make_fake_store()
    _seed_reservation(database, "res-race")
    _freeze(database, "res-race", "pending")

    real_select = mod._select_batch
    calls = {"n": 0}

    def select_then_release(store_arg: Any, batch: int) -> list[str]:
        ids = real_select(store_arg, batch)
        calls["n"] += 1
        if calls["n"] == 1:
            assert ids == [], "precondition: the frozen row is not selectable"
            # Concurrent human action: the guard status leaves GUARD_STATUSES.
            database.settle_outbox[("auth-res-race", "settle")]["status"] = (
                "release_approved"
            )
        return ids

    monkeypatch.setattr(mod, "_select_batch", select_then_release)

    assert run_backfill(store, apply=True) == 0

    assert database.reservations["res-race"]["terminal_at"] is not None
    output = capsys.readouterr().out
    assert "STOP" not in output
    assert output.endswith("COMPLETE: no unarmed settled reservations remain\n")
