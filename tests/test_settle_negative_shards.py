from __future__ import annotations

from typing import Any

import pytest

from scripts import settle_negative_shards as sweep
from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount

# workspace -> (credits per shard, usage per shard, marked)
SEED = {
    "ws-cover": ([0, 100], [50, 0], False),  # headroom [-50, 100]: covered
    "ws-mark": ([0, 100], [150, 0], False),  # [-150, 100]: the sum is negative, marked
    "ws-clear": ([50, 50], [0, 0], True),  # a stale mark: cleared
    "ws-keep": ([0, 100], [150, 0], True),  # a mark the balance bears out: kept
    "ws-healthy": ([10, 20], [0, 0], False),
    # A negative sum with one shard marked and one not: every shard marked.
    "ws-mixed": ([0, 100], [150, 0], [True, False]),
}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    # The fake snapshot accepts read options only in the hot path's form.
    monkeypatch.setattr(sweep, "_READ_OPTIONS", {})
    store, database = make_fake_store()
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for workspace_id, (credits, usage, marked) in SEED.items():
        store._write_entity(
            "credit", workspace_id, CreditAccount(workspace_id=workspace_id, shard_count=len(credits)),
        )
        for shard, total in enumerate(credits):
            table[(workspace_id, shard)] = {
                "workspace_id": workspace_id, "shard": shard, "total_credits": total,
                "total_usage": usage[shard], "reserved": 0,
                "in_debt": marked[shard] if isinstance(marked, list) else marked,
                "billing_pause_causes": [], "pause_epoch": 0,
                "source_updated_at": None, "updated_at": None,
            }
    return store, database


def _state(database: Any, workspace_id: str) -> tuple[list[int], list[bool]]:
    table = database.typed[CREDIT_BALANCE_TABLE]
    rows = [table[pk] for pk in sorted(pk for pk in table if pk[0] == workspace_id)]
    return (
        [row["total_credits"] - row["total_usage"] - row["reserved"] for row in rows],
        [bool(row["in_debt"]) for row in rows],
    )


def test_a_dry_run_reports_and_writes_nothing(store: tuple[Any, Any], capsys: Any) -> None:
    spanner_store, database = store
    before = {pk: dict(row) for pk, row in database.typed[CREDIT_BALANCE_TABLE].items()}
    assert sweep.run(spanner_store) == {"cover": 1, "mark": 2, "clear": 1}
    assert database.typed[CREDIT_BALANCE_TABLE] == before
    out = capsys.readouterr().out
    assert "DRY-RUN: cover ws-cover headroom=[-50, 100] sum=50 marks=[False, False]" in out
    assert "DRY-RUN: mark ws-mixed headroom=[-150, 100] sum=-50 marks=[True, False]" in out
    assert "ws-keep" not in out and "ws-healthy" not in out


def test_apply_covers_marks_and_clears_and_a_second_run_finds_nothing(
    store: tuple[Any, Any],
) -> None:
    spanner_store, database = store
    assert sweep.run(spanner_store, apply=True) == {"cover": 1, "mark": 2, "clear": 1, "unchanged": 0}
    assert _state(database, "ws-cover") == ([0, 50], [False, False])
    assert _state(database, "ws-mark") == ([-150, 100], [True, True])
    assert _state(database, "ws-mixed") == ([-150, 100], [True, True])
    assert _state(database, "ws-clear") == ([50, 50], [False, False])
    assert _state(database, "ws-keep") == ([-150, 100], [True, True])
    assert _state(database, "ws-healthy") == ([10, 20], [False, False])
    assert sweep.run(spanner_store, apply=True) == {"cover": 0, "mark": 0, "clear": 0, "unchanged": 0}


def test_a_shard_set_unlike_the_configured_one_is_skipped(
    store: tuple[Any, Any], capsys: Any,
) -> None:
    # Shard 1 is gone and shard 0 is negative: a candidate, but not a whole set.
    spanner_store, database = store
    database.typed[CREDIT_BALANCE_TABLE].pop(("ws-cover", 1))
    assert sweep.run(spanner_store, apply=True)["cover"] == 0
    assert "SKIP: ws-cover has shards [0], configured 2" in capsys.readouterr().out
    assert _state(database, "ws-cover") == ([-50], [False])


def test_a_missing_trailing_shard_is_skipped(store: tuple[Any, Any], capsys: Any) -> None:
    # Three shards configured, two rows present: the set is incomplete.
    spanner_store, database = store
    spanner_store._write_entity(
        "credit", "ws-cover", CreditAccount(workspace_id="ws-cover", shard_count=3),
    )
    assert sweep.run(spanner_store, apply=True)["cover"] == 0
    assert "SKIP: ws-cover has shards [0, 1], configured 3" in capsys.readouterr().out
    assert _state(database, "ws-cover") == ([-50, 100], [False, False])


def test_apply_counts_what_its_transactions_committed(
    store: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    # Money repairs ws-cover after the read and before its transaction.
    spanner_store, database = store
    stale = [w for w in sweep.find_workspaces(spanner_store) if w.workspace_id == "ws-cover"]
    database.typed[CREDIT_BALANCE_TABLE][("ws-cover", 0)]["total_credits"] = 50
    monkeypatch.setattr(sweep, "find_workspaces", lambda _store: stale)
    assert sweep.run(spanner_store, apply=True) == {"cover": 0, "mark": 0, "clear": 0, "unchanged": 1}
    assert "committed: nothing, already settled" in capsys.readouterr().out
    assert _state(database, "ws-cover") == ([0, 100], [False, False])
