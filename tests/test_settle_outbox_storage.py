"""Durable settle outbox — Increment 1: native-table storage layer.

Exercises the SpannerSettleOutbox primitives against the in-process Spanner fake
(which models tr_settle_outbox explicitly, so a guard/status/column mistake fails
here rather than silently passing). The reaper-guard wiring, enqueue-at-settle,
drain worker, and frozen-cost finalize primitive land in later increments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes.spanner import _execute_settle_outbox_sql, _FakeTransaction, make_fake_store
from trusted_router.storage_gcp_settle_outbox import (
    _GUARD_STATUS_SQL,
    ENQ_EXISTS_TERMINAL,
    ENQ_INSERTED,
    ENQ_LEASED,
    ENQ_REFRESHED,
    OUTBOX_COLUMNS,
    SpannerSettleOutbox,
)
from trusted_router.storage_models import SettleOutboxRow


def _outbox(store) -> SpannerSettleOutbox:
    return SpannerSettleOutbox(store._database, store._param_types)


def _row(aid: str, *, kind: str = "settle", cost: int = 1000, origin: str = "typed") -> SettleOutboxRow:
    return SettleOutboxRow(
        authorization_id=aid,
        intent_kind=kind,
        settle_origin=origin,
        actual_cost_micro=cost,
        reservation_id=f"res-{aid}",
        selected_endpoint_id="openai/gpt-4o@openai",
        model_id="openai/gpt-4o",
        selected_usage_type="Credits",
        settle_body=f'{{"authorization_id":"{aid}"}}',
    )


def test_enqueue_inserts_and_get_returns_frozen_inputs() -> None:
    store, database, _ = make_fake_store()
    ob = _outbox(store)
    assert ob.enqueue(_row("gwa-1", cost=4200)) == ENQ_INSERTED
    got = ob.get("gwa-1", "settle")
    assert got is not None
    assert got.status == "pending"
    assert got.actual_cost_micro == 4200  # frozen
    assert got.settle_origin == "typed"
    assert got.reservation_id == "res-gwa-1"
    assert got.selected_usage_type == "Credits"
    assert 0 <= database.settle_outbox[("gwa-1", "settle")]["queue_shard"] < 16


def test_enqueue_is_idempotent_and_refreshes_a_pending_row() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    assert ob.enqueue(_row("gwa-2", cost=1000)) == ENQ_INSERTED
    # A retry with corrected actuals updates the still-pending row (SF9), one row.
    assert ob.enqueue(_row("gwa-2", cost=1750)) == ENQ_REFRESHED
    got = ob.get("gwa-2", "settle")
    assert got is not None and got.actual_cost_micro == 1750


def test_enqueue_does_not_clobber_a_terminal_row() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-3", cost=1000))
    assert ob.mark("gwa-3", "settle", done=True) == "done"
    # Re-enqueue after the charge applied must NOT reopen or overwrite it.
    assert ob.enqueue(_row("gwa-3", cost=9999)) == ENQ_EXISTS_TERMINAL
    got = ob.get("gwa-3", "settle")
    assert got is not None and got.status == "done" and got.actual_cost_micro == 1000


def test_settle_and_refund_are_separate_rows_same_authorization() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-4", kind="settle", cost=500))
    ob.enqueue(_row("gwa-4", kind="refund", cost=0))
    assert ob.get("gwa-4", "settle").actual_cost_micro == 500
    assert ob.get("gwa-4", "refund").actual_cost_micro == 0


def test_due_then_claim_leases_and_second_claim_skips() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-5"))
    assert [r.authorization_id for r in ob.due()] == ["gwa-5"]
    claimed = ob.claim(lease_seconds=300)
    assert [r.authorization_id for r in claimed] == ["gwa-5"]
    # The lease is live -> a second claimer gets nothing (no double-drain).
    assert ob.claim(lease_seconds=300) == []


def test_mark_done_settles_and_drops_out_of_due() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-6"))
    [job] = ob.claim(lease_seconds=300)
    assert ob.mark("gwa-6", "settle", done=True, lease_owner=job.lease_owner) == "done"
    assert ob.due() == []
    done = ob.get("gwa-6", "settle")
    assert done is not None
    assert done.status == "done"
    assert done.next_attempt_at is None


def test_mark_failure_backs_off_then_dies_at_max_attempts() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-7"))
    # First failure -> pending, attempts=1, next_attempt in the future (not due now).
    assert ob.mark("gwa-7", "settle", done=False, error="boom", max_attempts=3) == "pending"
    got = ob.get("gwa-7", "settle")
    assert got.status == "pending" and got.attempts == 1 and got.last_error == "boom"
    assert ob.due() == []  # backed off
    # Drive to max_attempts -> dead (which FREEZES the hold for a human).
    assert ob.mark("gwa-7", "settle", done=False, max_attempts=3) == "pending"
    assert ob.mark("gwa-7", "settle", done=False, max_attempts=3) == "dead"
    assert ob.get("gwa-7", "settle").status == "dead"


def test_mark_rejects_a_lost_lease() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-8"))
    ob.claim(lease_seconds=300)  # owned by worker A
    # Worker B (wrong owner) cannot mark it.
    assert ob.mark("gwa-8", "settle", done=True, lease_owner="soworker_intruder") is None
    assert ob.get("gwa-8", "settle").status == "pending"


def test_stale_mark_after_reclaim_and_park_is_rejected() -> None:
    store, db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-stale-mark"))
    [job] = ob.claim(lease_seconds=300)
    record = db.settle_outbox[("gwa-stale-mark", "settle")]
    record["lease_owner"] = None
    record["leased_until"] = None

    assert (
        ob.mark(
            "gwa-stale-mark",
            "settle",
            done=False,
            error="stale terminal outcome",
            lease_owner=job.lease_owner,
            force_dead=True,
        )
        is None
    )
    pending = ob.get("gwa-stale-mark", "settle")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.last_error is None


def test_stale_park_after_reclaim_and_park_is_rejected() -> None:
    store, db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-stale-park"))
    [job] = ob.claim(lease_seconds=300)
    record = db.settle_outbox[("gwa-stale-park", "settle")]
    record["lease_owner"] = None
    record["leased_until"] = None
    attempts_before = record["attempts"]
    next_attempt_before = record["next_attempt_at"]

    assert (
        ob.park(
            "gwa-stale-park",
            "settle",
            lease_owner=job.lease_owner,
            retry_after_seconds=120,
            note="stale park",
        )
        is False
    )
    pending = ob.get("gwa-stale-park", "settle")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.attempts == attempts_before
    assert pending.next_attempt_at == next_attempt_before
    assert pending.last_error is None


def test_anonymous_mark_requires_an_unleased_row() -> None:
    store, db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-anonymous-fence"))
    [job] = ob.claim(lease_seconds=300)

    assert ob.mark("gwa-anonymous-fence", "settle", done=True) is None
    fenced = ob.get("gwa-anonymous-fence", "settle")
    assert fenced is not None
    assert fenced.status == "pending"
    assert fenced.lease_owner == job.lease_owner

    record = db.settle_outbox[("gwa-anonymous-fence", "settle")]
    record["lease_owner"] = None
    record["leased_until"] = None
    assert ob.mark("gwa-anonymous-fence", "settle", done=True) == "done"


def test_enqueue_initial_delay_defers_claim_until_default_row_is_due() -> None:
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    assert ob.enqueue(_row("gwa-delayed"), initial_delay_seconds=60) == ENQ_INSERTED
    assert ob.claim() == []

    assert ob.enqueue(_row("gwa-now")) == ENQ_INSERTED
    claimed = ob.claim()
    assert [row.authorization_id for row in claimed] == ["gwa-now"]
    delayed = ob.get("gwa-delayed", "settle")
    assert delayed is not None
    assert delayed.status == "pending"
    assert delayed.lease_owner is None


def test_enqueue_refresh_does_not_overwrite_an_actively_leased_row() -> None:
    """codex #113 finding 2: a claimed row stays status='pending' while a drain
    applies it, so a retry-enqueue must NOT overwrite its frozen inputs mid-drain."""
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    ob.enqueue(_row("gwa-11", cost=1000))
    [job] = ob.claim(lease_seconds=300)  # a drain worker now owns it
    # The enclave re-delivers with corrected actuals while the drain holds the lease.
    assert ob.enqueue(_row("gwa-11", cost=8888)) == ENQ_LEASED  # deferred, not terminal
    got = ob.get("gwa-11", "settle")
    assert got.actual_cost_micro == 1000  # unchanged under the active lease
    assert got.lease_owner == job.lease_owner


def test_fake_is_sql_sensitive_dropped_predicate_fails() -> None:
    """MF6 / codex #113 finding 1: the fake must FAIL when a load-bearing SQL
    predicate is dropped, not silently enforce the intended behavior in Python."""
    store, db, _ = make_fake_store()
    _outbox(store).enqueue(_row("gwa-12"))
    # A has_intent query missing `authorization_id=@aid` must FAIL (not count).
    with pytest.raises(AssertionError, match="has_intent"):
        _execute_settle_outbox_sql(
            db, None,
            f"SELECT COUNT(*) FROM tr_settle_outbox WHERE status IN ({_GUARD_STATUS_SQL})",  # noqa: S608
            {"aid": "gwa-12"},
        )
    # A claim query missing its `leased_until` lease fence must likewise FAIL.
    with pytest.raises(AssertionError, match="claim"):
        _FakeTransaction(db).execute_update(
            "UPDATE tr_settle_outbox SET lease_owner=@owner, leased_until=@lease, "
            "updated_at=@now WHERE authorization_id=@aid AND intent_kind=@kind "
            "AND status='pending'",  # dropped the leased_until fence
            params={"owner": "x", "lease": "z", "now": "z", "aid": "gwa-12", "kind": "settle"},
        )
    # A due query must stay pinned to the sparse sharded index. Accidentally
    # dropping the hint can silently restore the production moving-edge scan.
    with pytest.raises(AssertionError, match="due-scan-index"):
        _execute_settle_outbox_sql(
            db,
            None,
            f"SELECT {', '.join(OUTBOX_COLUMNS)} FROM tr_settle_outbox "  # noqa: S608
            "WHERE queue_shard IS NOT NULL AND next_attempt_at IS NOT NULL "
            "AND status='pending' AND next_attempt_at <= @now "
            "ORDER BY next_attempt_at LIMIT @limit",
            {"now": "z", "limit": 10},
        )
    # A mark query missing the PK key predicate must FAIL — real Spanner would
    # update every matching pending row, not the single pk (codex #113 re-review).
    with pytest.raises(AssertionError, match="mark"):
        _FakeTransaction(db).execute_update(
            "UPDATE tr_settle_outbox SET status=@status, attempts=@attempts, "
            "last_error=@err, next_attempt_at=@next_at, lease_owner=NULL, "
            "leased_until=NULL, updated_at=@now WHERE status='pending'",  # dropped the PK
            params={"status": "done", "attempts": 1, "err": None, "next_at": None,
                    "now": "z", "aid": "gwa-12", "kind": "settle"},
        )
    # Symmetric coverage for the other two UPDATE handlers missing the PK filter.
    with pytest.raises(AssertionError, match="refresh"):
        _FakeTransaction(db).execute_update(
            "UPDATE tr_settle_outbox SET settle_origin=@settle_origin, "
            "reservation_id=@reservation_id, actual_cost_micro=@actual_cost_micro, "
            "selected_endpoint_id=@selected_endpoint_id, model_id=@model_id, "
            "selected_usage_type=@selected_usage_type, settle_body=@settle_body, "
            "updated_at=@now WHERE status='pending' "
            "AND (leased_until IS NULL OR leased_until < @now)",  # dropped the PK
            params={"settle_origin": "typed", "reservation_id": None,
                    "actual_cost_micro": 1, "selected_endpoint_id": None, "model_id": None,
                    "selected_usage_type": None, "settle_body": None, "now": "z",
                    "authorization_id": "gwa-12", "intent_kind": "settle"},
        )
    with pytest.raises(AssertionError, match="claim"):
        _FakeTransaction(db).execute_update(
            "UPDATE tr_settle_outbox SET lease_owner=@owner, leased_until=@lease, "
            "updated_at=@now WHERE status='pending' "
            "AND (leased_until IS NULL OR leased_until < @now)",  # dropped the PK
            params={"owner": "x", "lease": "z", "now": "z", "aid": "gwa-12", "kind": "settle"},
        )


def test_has_intent_freezes_on_pending_and_dead_only() -> None:
    store, db, _ = make_fake_store()
    ob = _outbox(store)
    assert ob.has_intent("absent") is False
    ob.enqueue(_row("gwa-9"))
    assert ob.has_intent("gwa-9") is True  # pending freezes
    ob.mark("gwa-9", "settle", done=True)
    assert ob.has_intent("gwa-9") is False  # done does NOT freeze (charge applied)
    # dead freezes (drain gave up, human must resolve).
    ob.enqueue(_row("gwa-10"))
    for _ in range(8):
        ob.mark("gwa-10", "settle", done=False, max_attempts=8)
    assert ob.get("gwa-10", "settle").status == "dead"
    assert ob.has_intent("gwa-10") is True
    # release_approved (human ok'd freeing) does NOT freeze.
    db.settle_outbox[("gwa-10", "settle")]["status"] = "release_approved"
    assert ob.has_intent("gwa-10") is False


def test_outbox_schema_uses_generated_shard_and_sparse_due_index() -> None:
    root = Path(__file__).parents[1]
    migration = (root / "scripts/deploy/migrate_typed_counters.sh").read_text()
    retirement = (
        root / "scripts/deploy/retire_settle_outbox_hot_index.sh"
    ).read_text()
    workflow = (root / ".github/workflows/deploy.yml").read_text()

    assert "queue_shard INT64 NOT NULL AS (" in migration
    assert "FARM_FINGERPRINT(CONCAT(authorization_id, '#', intent_kind))" in migration
    assert "CREATE NULL_FILTERED INDEX tr_settle_outbox_due_v2" in migration
    assert "ON tr_settle_outbox (queue_shard, next_attempt_at)" in migration
    assert "CREATE INDEX tr_settle_outbox_due ON" not in migration
    assert migration.index(
        "wait_generated_column_committed tr_settle_outbox queue_shard"
    ) < migration.index("CREATE NULL_FILTERED INDEX tr_settle_outbox_due_v2")
    assert migration.index("wait_index_read_write tr_settle_outbox_due_v2") > (
        migration.index("CREATE NULL_FILTERED INDEX tr_settle_outbox_due_v2")
    )
    assert "index_state='READ_WRITE'" in retirement
    assert retirement.index("tr_settle_outbox_due_v2") < retirement.index(
        "DROP INDEX tr_settle_outbox_due"
    )
    assert retirement.index("queue_shard IS NULL") < retirement.index(
        "DROP INDEX tr_settle_outbox_due"
    )
    assert workflow.index("Smoke test prod") < workflow.index(
        "Retire legacy settle-outbox hotspot index"
    )
