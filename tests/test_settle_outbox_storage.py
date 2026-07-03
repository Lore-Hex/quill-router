"""Durable settle outbox — Increment 1: native-table storage layer.

Exercises the SpannerSettleOutbox primitives against the in-process Spanner fake
(which models tr_settle_outbox explicitly, so a guard/status/column mistake fails
here rather than silently passing). The reaper-guard wiring, enqueue-at-settle,
drain worker, and frozen-cost finalize primitive land in later increments.
"""

from __future__ import annotations

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_settle_outbox import (
    ENQ_EXISTS_TERMINAL,
    ENQ_INSERTED,
    ENQ_REFRESHED,
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
    store, _db, _ = make_fake_store()
    ob = _outbox(store)
    assert ob.enqueue(_row("gwa-1", cost=4200)) == ENQ_INSERTED
    got = ob.get("gwa-1", "settle")
    assert got is not None
    assert got.status == "pending"
    assert got.actual_cost_micro == 4200  # frozen
    assert got.settle_origin == "typed"
    assert got.reservation_id == "res-gwa-1"
    assert got.selected_usage_type == "Credits"


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
    assert ob.get("gwa-6", "settle").status == "done"


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
