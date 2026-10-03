"""The tier job's bulk selection, and the shadow that checks it.

The job decides each workspace with several reads and, when something changed,
a transaction: on 2026-10-02, 1,596 workspaces took about 13 minutes, almost
all of it round trips. ``trust_tier_bulk`` reads the same rows once and runs
the same evaluator against them. Until the job switches to it, the shadow runs
the bulk selection before the per-workspace pass and reports any workspace the
pass changed that the selection missed, as a race (its inputs changed after the
snapshot) or a defect.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from tests.fakes.spanner import make_fake_store
from trusted_router import trust_tier_bulk, trust_tier_cli
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_trust import evaluate_workspace_trust_tier
from trusted_router.storage_models import CreditAccount, CreditProvenance, User, Workspace
from trusted_router.storage_trust_reconciliation import (
    SHARD_WATERMARKS_SQL,
    read_expected_reconciled_through,
)
from trusted_router.trust_reconciliation import STRIPE_TRUST_SOURCE, STRIPE_TRUST_SOURCE_VERSION
from trusted_router.trust_tier_bulk import (
    BulkWorkspaceReader,
    iter_trust_tier_bulk,
    select_trust_tier_candidates,
)

NOW = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
MARKED = DatetimeWithNanoseconds(2026, 9, 3, 11, 0, 0, nanosecond=2, tzinfo=dt.UTC)
PAID = 50_000_000
_SOURCES = {"stripe": "checkout", "x402": "x402"}
POLICY: dict[str, Any] = {
    "qualifying_providers": frozenset({"stripe", "x402"}),
    "tier3_min_days": 30,
    "tier3_min_paid_microdollars": PAID,
}
SETTINGS = SimpleNamespace(
    trust_qualifying_provider_set=POLICY["qualifying_providers"],
    trust_tier3_min_days=POLICY["tier3_min_days"],
    trust_tier3_min_paid_microdollars=PAID,
    trust_tier_job_concurrency=1,
    trust_tier_shadow_enabled=True,
)
FAILING = {"ws-missing-shard", "ws-latch-diverge", "ws-override-mismatch", "ws-malformed"}
ACTED = {
    "ws-stale-tier",
    "ws-null-computed",
    "ws-watermark-stale",
    "ws-watermark-nanos",
    "ws-two-markers",
}


def _marker(database: Any, provider: str, account: str, closed_through: dt.datetime) -> None:
    table = database.typed.setdefault("tr_trust_backfill", {})
    key = (provider, account, "production", STRIPE_TRUST_SOURCE, STRIPE_TRUST_SOURCE_VERSION)
    table[key] = {
        "provider": provider,
        "account_id": account,
        "environment": "production",
        "source": STRIPE_TRUST_SOURCE,
        "source_version": STRIPE_TRUST_SOURCE_VERSION,
        "history_start": NOW - dt.timedelta(days=400),
        "closed_through": closed_through,
        "consistency_delay_seconds": 0,
        "unmatched_count": 0,
        "semantic_mismatch_count": 0,
        "completed_at": NOW - dt.timedelta(minutes=30),
    }


def _workspace(
    store: Any,
    database: Any,
    ws: str,
    *,
    owner: str = "owner",
    shards: int = 2,
    provider: str | None = "stripe",
    watermark: Any = MARKED,
    settle: bool = True,
) -> None:
    store._write_entity("workspace", ws, Workspace(id=ws, name=ws, owner_user_id=owner))
    store._write_entity("credit", ws, CreditAccount(workspace_id=ws, shard_count=shards))
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard in range(shards):
        table[(ws, shard)] = {
            "workspace_id": ws,
            "shard": shard,
            "total_credits": 0,
            "total_usage": 0,
            "reserved": 0,
            "trust_tier": 0,
            "trust_computed_at": None,
            "trust_latched_at": None,
            "trust_override_tier": None,
            "billing_pause_causes": [],
            "pause_epoch": 0,
            "trust_reconciled_through": watermark,
        }
    if provider is not None:
        assert store.credit_workspace_typed_direct(
            ws,
            PAID,
            f"pay-{ws}",
            provenance=CreditProvenance(
                _SOURCES[provider], provider, f"pi_{ws}", NOW - dt.timedelta(days=31)
            ),
            payment_amount_microdollars=PAID,
            currency="USD",
        )
    if settle:
        # Bring the stored tier up to date, so only the deliberate change differs.
        store.recompute_workspace_trust_tier(ws, now=NOW, **POLICY)


def _fleet() -> tuple[Any, Any]:
    store, database = make_fake_store()
    store._write_entity(
        "user", "owner", User(id="owner", email="owner@example.com", identity_status="approved")
    )
    _marker(database, "stripe", "acct_1", MARKED)
    _marker(database, "x402", "acct_1", MARKED)
    _marker(database, "x402", "acct_2", MARKED)  # two matching markers: expected NULL
    balance = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})

    _workspace(store, database, "ws-current")
    _workspace(store, database, "ws-no-owner", owner="ghost", provider=None, watermark=None)

    _workspace(store, database, "ws-stale-tier", settle=False)
    _workspace(store, database, "ws-null-computed")
    balance[("ws-null-computed", 1)]["trust_computed_at"] = None
    _workspace(store, database, "ws-watermark-stale", watermark=None)
    _workspace(
        store,
        database,
        "ws-watermark-nanos",
        watermark=DatetimeWithNanoseconds(2026, 9, 3, 11, 0, 0, nanosecond=1, tzinfo=dt.UTC),
    )
    _workspace(store, database, "ws-two-markers", provider="x402")

    _workspace(store, database, "ws-missing-shard")
    del balance[("ws-missing-shard", 1)]
    _workspace(store, database, "ws-latch-diverge")
    balance[("ws-latch-diverge", 0)]["trust_latched_at"] = NOW
    _workspace(store, database, "ws-override-mismatch")
    database.typed.setdefault("tr_trust_override", {})[("ws-override-mismatch",)] = {
        "workspace_id": "ws-override-mismatch",
        "tier": 1,
        "identity_bypass": False,
    }
    _workspace(store, database, "ws-malformed")
    database.rows[("workspace", "ws-malformed")].body = "{not json"
    return store, database


def _select(store: Any, *, chunk_size: int = 1_000) -> Any:
    return select_trust_tier_candidates(
        iter_trust_tier_bulk(
            store._database, store._param_types, environment="production", chunk_size=chunk_size
        ),
        param_types=store._param_types,
        read_entity_tx=store._read_entity_tx,
        now=NOW,
        **POLICY,
    )


def _after_selection(
    monkeypatch: Any, change: Callable[[], None] = lambda: None, *, drop: str | None = None
) -> None:
    """Run ``change`` once the shadow's selection is complete, and optionally
    drop a candidate from it, as a selection bug would."""

    select = trust_tier_cli.select_trust_tier_candidates

    def select_then_change(*args: Any, **kwargs: Any) -> Any:
        selection = select(*args, **kwargs)
        if drop is not None:
            selection.candidates.pop(drop)
        change()
        return selection

    monkeypatch.setattr(trust_tier_cli, "select_trust_tier_candidates", select_then_change)


def _defect_lines(caplog: Any) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "trust.tier_shadow_defect" in r.getMessage()
    ]


def _summary(caplog: Any) -> dict[str, int]:
    lines = [
        r.getMessage() for r in caplog.records if "trust.tier_shadow_complete" in r.getMessage()
    ]
    assert len(lines) == 1, lines
    return {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", lines[0])}


def test_the_selection_is_exactly_what_the_pass_would_act_on() -> None:
    store, _ = _fleet()
    selection = _select(store)

    assert set(selection.candidates) == ACTED | FAILING
    assert selection.candidates["ws-stale-tier"] == ("tier",)
    assert selection.candidates["ws-null-computed"] == ("tier",)
    assert selection.candidates["ws-watermark-stale"] == ("watermark",)
    assert selection.candidates["ws-watermark-nanos"] == ("watermark",)
    assert selection.candidates["ws-two-markers"] == ("watermark",)
    for ws in FAILING:
        assert selection.candidates[ws][0].startswith("tier_evaluation_failed:")


def test_the_shadow_finds_no_defect_when_the_pass_matches(caplog: Any) -> None:
    store, _ = _fleet()
    with caplog.at_level(logging.INFO):
        result = trust_tier_cli.run(store, SETTINGS, now=NOW)

    assert set(result.failed) == FAILING
    summary = _summary(caplog)
    assert summary["defects"] == 0
    assert summary["races"] == 0
    assert summary["failed_outside"] == 0
    # Written or refused: a refusal is reported with the rows it read.
    assert summary["acted"] == len(ACTED | FAILING)
    assert summary["unacted_candidates"] == 0


@pytest.mark.parametrize("ws", sorted(ACTED | FAILING | {"ws-current", "ws-no-owner"}))
def test_the_bulk_reader_gives_the_evaluator_the_same_rows(ws: str) -> None:
    store, database = _fleet()
    [bulk] = iter_trust_tier_bulk(store._database, store._param_types, environment="production")

    def decide(reader: Any) -> Any:
        try:
            tier, rows, count, digest = evaluate_workspace_trust_tier(
                reader,
                param_types=store._param_types,
                read_entity_tx=store._read_entity_tx,
                workspace_id=ws,
                now=NOW,
                **POLICY,
            )
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__
        return tier, [list(row) for row in rows], count, digest

    def watermark(reader: Any) -> Any:
        expected = read_expected_reconciled_through(
            reader, store._param_types, ws, POLICY["qualifying_providers"], environment="production"
        )
        current = [
            list(row)
            for row in reader.execute_sql(SHARD_WATERMARKS_SQL, params={"workspace_id": ws})
        ]
        return expected, current

    with database.snapshot(multi_use=True) as snapshot:
        direct = decide(snapshot), watermark(snapshot)
    assert (
        decide(BulkWorkspaceReader(bulk, ws)),
        watermark(BulkWorkspaceReader(bulk, ws)),
    ) == direct


def test_a_no_op_fallback_write_is_not_a_defect(caplog: Any, monkeypatch: Any) -> None:
    store, database = _fleet()
    real_snapshot = database.snapshot

    def failing_snapshot(*args: Any, **kwargs: Any) -> Any:
        # The per-workspace prechecks read on multi-use snapshots; the job's
        # workspace list does not, and must keep working.
        if kwargs.get("multi_use"):
            raise RuntimeError("snapshot unavailable")
        return real_snapshot(*args, **kwargs)

    # Every per-workspace precheck now fails, so each falls through to its
    # transaction, which rewrites a watermark that is already current.
    _after_selection(monkeypatch, lambda: monkeypatch.setattr(database, "snapshot", failing_snapshot))
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, SETTINGS, now=NOW)
    monkeypatch.setattr(database, "snapshot", real_snapshot)

    messages = [r.getMessage() for r in caplog.records]
    # Positive control: the fallback really ran for an unchanged workspace.
    assert any(
        "trust.watermark_snapshot_precheck_failed workspace_id=ws-current" in m for m in messages
    )
    summary = _summary(caplog)
    assert summary["defects"] == 0
    assert summary["races"] == 0


def test_an_input_changed_after_the_snapshot_is_a_race(caplog: Any, monkeypatch: Any) -> None:
    store, database = _fleet()

    def change() -> None:
        for shard in range(2):
            database.typed[CREDIT_BALANCE_TABLE][("ws-current", shard)]["trust_tier"] = 1

    _after_selection(monkeypatch, change)
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, SETTINGS, now=NOW)

    summary = _summary(caplog)
    assert summary["races"] == 1
    assert summary["defects"] == 0
    assert any(
        "trust.tier_shadow_race workspace_id=ws-current" in r.getMessage() for r in caplog.records
    )


def test_a_selection_that_misses_a_change_is_a_defect(caplog: Any, monkeypatch: Any) -> None:
    """The shadow's positive control: a selection bug is reported, not hidden."""

    store, _ = _fleet()
    _after_selection(monkeypatch, drop="ws-stale-tier")
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, SETTINGS, now=NOW)

    summary = _summary(caplog)
    assert summary["defects"] == 1
    assert any(
        r.levelno == logging.ERROR
        and "trust.tier_shadow_defect workspace_id=ws-stale-tier" in r.getMessage()
        for r in caplog.records
    )


def test_an_unknown_query_makes_the_workspace_a_candidate(monkeypatch: Any) -> None:
    store, _ = _fleet()
    evaluate = trust_tier_bulk.evaluate_workspace_trust_tier

    def evaluate_with_a_new_read(reader: Any, **kwargs: Any) -> Any:
        reader.execute_sql("SELECT something_new FROM tr_credit_balance")
        return evaluate(reader, **kwargs)

    monkeypatch.setattr(trust_tier_bulk, "evaluate_workspace_trust_tier", evaluate_with_a_new_read)
    selection = _select(store)

    assert selection.candidates["ws-current"] == ("tier_evaluation_failed:UnsupportedBulkQuery",)


def test_a_failing_shadow_never_changes_the_pass(caplog: Any, monkeypatch: Any) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bulk read failed")

    store, database = _fleet()
    monkeypatch.setattr(trust_tier_cli, "iter_trust_tier_bulk", broken)
    with caplog.at_level(logging.INFO):
        shadowed = trust_tier_cli.run(store, SETTINGS, now=NOW)
    plain_store, plain_database = _fleet()
    off = SimpleNamespace(**{**vars(SETTINGS), "trust_tier_shadow_enabled": False})
    plain = trust_tier_cli.run(plain_store, off, now=NOW)

    assert any("trust.tier_shadow_unavailable" in r.getMessage() for r in caplog.records)
    assert shadowed == plain
    assert database.typed[CREDIT_BALANCE_TABLE] == plain_database.typed[CREDIT_BALANCE_TABLE]


def test_the_shadow_can_be_turned_off(caplog: Any) -> None:
    store, _ = _fleet()
    off = SimpleNamespace(**{**vars(SETTINGS), "trust_tier_shadow_enabled": False})
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, off, now=NOW)

    assert not any("trust.tier_shadow" in r.getMessage() for r in caplog.records)


def test_the_bulk_event_read_has_the_evaluators_columns() -> None:
    from trusted_router.storage_gcp_trust import TRUST_EVENTS_SQL

    def columns(sql: str) -> list[str]:
        return [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]

    assert columns(trust_tier_bulk.BULK_EVENTS_SQL) == ["workspace_id", *columns(TRUST_EVENTS_SQL)]


def test_a_watermark_race_does_not_hide_a_missed_tier_write(caplog: Any, monkeypatch: Any) -> None:
    """Each write is judged on its own inputs, not the workspace as a whole."""

    store, database = _fleet()
    later = DatetimeWithNanoseconds(2026, 9, 3, 11, 30, 0, nanosecond=2, tzinfo=dt.UTC)
    # The selection drops a real tier candidate, and a marker advance after the
    # snapshot gives the same workspace a watermark write that did race.
    _after_selection(
        monkeypatch, lambda: _marker(database, "stripe", "acct_1", later), drop="ws-stale-tier"
    )
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, SETTINGS, now=NOW)

    assert _summary(caplog)["defects"] == 1
    [line] = _defect_lines(caplog)
    assert "workspace_id=ws-stale-tier missed=tier_written acted=tier_written,watermark_changed" in line


def test_a_stored_watermark_changed_after_the_snapshot_is_a_race(caplog: Any, monkeypatch: Any) -> None:
    store, database = _fleet()

    def change() -> None:
        database.typed[CREDIT_BALANCE_TABLE][("ws-current", 1)]["trust_reconciled_through"] = None

    _after_selection(monkeypatch, change)
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, SETTINGS, now=NOW)

    summary = _summary(caplog)
    assert summary["defects"] == 0
    assert summary["races"] == 1
    assert any(
        "trust.tier_shadow_race workspace_id=ws-current acted=watermark_changed" in r.getMessage()
        for r in caplog.records
    )


def test_a_marker_moved_by_a_nanosecond_is_a_race(caplog: Any, monkeypatch: Any) -> None:
    store, database = _fleet()
    moved = DatetimeWithNanoseconds(2026, 9, 3, 11, 0, 0, nanosecond=3, tzinfo=dt.UTC)
    _after_selection(monkeypatch, lambda: _marker(database, "stripe", "acct_1", moved))
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, SETTINGS, now=NOW)

    summary = _summary(caplog)
    assert summary["defects"] == 0
    # Positive control: the move really made the pass rewrite a watermark.
    assert any(
        "trust.tier_shadow_race workspace_id=ws-current acted=watermark_changed" in r.getMessage()
        for r in caplog.records
    )


def test_a_dropped_refusal_is_a_defect(caplog: Any, monkeypatch: Any) -> None:
    """A workspace the evaluator refuses on unchanged rows must be selected."""

    store, _ = _fleet()
    _after_selection(monkeypatch, drop="ws-missing-shard")
    with caplog.at_level(logging.INFO):
        result = trust_tier_cli.run(store, SETTINGS, now=NOW)

    assert "ws-missing-shard" in result.failed
    summary = _summary(caplog)
    assert summary["defects"] == 1
    assert summary["failed_outside"] == 0
    [line] = _defect_lines(caplog)
    assert "workspace_id=ws-missing-shard missed=tier_refused" in line


def test_a_refusal_that_raced_the_snapshot_is_not_a_defect(caplog: Any, monkeypatch: Any) -> None:
    store, database = _fleet()
    _after_selection(
        monkeypatch, lambda: database.typed[CREDIT_BALANCE_TABLE].pop(("ws-current", 1))
    )
    with caplog.at_level(logging.INFO):
        result = trust_tier_cli.run(store, SETTINGS, now=NOW)

    assert "ws-current" in result.failed
    summary = _summary(caplog)
    assert summary["defects"] == 0
    assert any(
        "trust.tier_shadow_race workspace_id=ws-current acted=tier_refused" in r.getMessage()
        for r in caplog.records
    )


def test_a_failed_read_is_not_a_refusal(caplog: Any, monkeypatch: Any) -> None:
    """A read that fails says nothing about the inputs, so it is no evidence."""

    from google.api_core.exceptions import ServiceUnavailable

    from tests.fakes import spanner as fake_spanner
    from trusted_router.storage_gcp_trust import TRUST_EVENTS_SQL

    store, _ = _fleet()

    def unavailable_for(cls: Any) -> Any:
        execute = cls.execute_sql

        def execute_sql(self: Any, sql: str, **kwargs: Any) -> Any:
            if sql == TRUST_EVENTS_SQL and (kwargs.get("params") or {}).get("pk") == "ws-current":
                raise ServiceUnavailable("spanner unavailable")
            return execute(self, sql, **kwargs)

        return execute_sql

    def change() -> None:
        for cls in (fake_spanner._FakeSnapshot, fake_spanner._FakeTransaction):
            monkeypatch.setattr(cls, "execute_sql", unavailable_for(cls))

    _after_selection(monkeypatch, change)
    with caplog.at_level(logging.INFO):
        result = trust_tier_cli.run(store, SETTINGS, now=NOW)

    assert "ws-current" in result.failed
    summary = _summary(caplog)
    assert summary["defects"] == 0
    assert summary["races"] == 0
    assert summary["failed_outside"] == 1


def test_chunks_read_one_snapshot_and_judge_every_workspace_once(monkeypatch: Any) -> None:
    store, database = _fleet()
    whole = _select(store)
    seen_chunks: list[set[str]] = []
    real = trust_tier_bulk.BulkWorkspaceReader

    def recording_reader(bulk: Any, workspace_id: str) -> Any:
        if not seen_chunks or seen_chunks[-1] != set(bulk.balances):
            seen_chunks.append(set(bulk.balances))
        return real(bulk, workspace_id)

    monkeypatch.setattr(trust_tier_bulk, "BulkWorkspaceReader", recording_reader)
    snapshots = len([call for call in database.snapshot_calls if call.get("multi_use")])
    chunked = _select(store, chunk_size=2)

    assert len([call for call in database.snapshot_calls if call.get("multi_use")]) == snapshots + 1
    assert all(len(chunk) <= 2 for chunk in seen_chunks)
    assert sorted(ws for chunk in seen_chunks for ws in chunk) == sorted(whole.workspaces)
    assert chunked.workspaces == whole.workspaces
    assert chunked.candidates == whole.candidates
    assert chunked.digests == whole.digests


def test_a_chunk_holds_only_its_own_workspaces_rows() -> None:
    store, _ = _fleet()
    chunks = list(
        iter_trust_tier_bulk(store._database, store._param_types, environment="production", chunk_size=3)
    )

    assert len(chunks) > 1
    for chunk in chunks:
        workspaces = set(chunk.balances)
        assert len(workspaces) <= 3
        assert set(chunk.overrides) <= workspaces
        assert set(chunk.events) <= workspaces
        assert {entity_id for kind, entity_id in chunk.entities if kind != "user"} <= workspaces


def test_marker_order_does_not_change_a_digest(caplog: Any, monkeypatch: Any) -> None:
    """Two markers that differ only below a microsecond digest the same in either
    order, so a dropped candidate between them is still a defect."""

    from trusted_router.storage_trust_reconciliation import MATCHING_MARKERS_SQL

    store, database = _fleet()
    _marker(database, "x402", "acct_2", DatetimeWithNanoseconds(
        2026, 9, 3, 11, 0, 0, nanosecond=3, tzinfo=dt.UTC,
    ))
    answer = trust_tier_bulk.BulkWorkspaceReader.answer

    def reversed_markers(self: Any, sql: str, params: Any = None, param_types: Any = None) -> Any:
        rows = answer(self, sql, params, param_types)
        return rows[::-1] if sql == MATCHING_MARKERS_SQL else rows

    # The snapshot reads the markers in the opposite order to the pass.
    monkeypatch.setattr(trust_tier_bulk.BulkWorkspaceReader, "answer", reversed_markers)
    monkeypatch.setattr(trust_tier_bulk.BulkWorkspaceReader, "execute_sql", reversed_markers)
    _after_selection(monkeypatch, drop="ws-two-markers")
    with caplog.at_level(logging.INFO):
        trust_tier_cli.run(store, SETTINGS, now=NOW)

    assert _summary(caplog)["defects"] == 1
    [line] = _defect_lines(caplog)
    assert "workspace_id=ws-two-markers missed=watermark_changed" in line
