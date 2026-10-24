"""The fake Spanner must model real-Spanner semantics that have leaked prod
bugs — otherwise green tests give false safety on the money path."""

from __future__ import annotations

import pytest

from tests.fakes.spanner import FakeSpannerDatabase


def _db() -> FakeSpannerDatabase:
    return FakeSpannerDatabase()


def test_single_use_snapshot_raises_on_second_read() -> None:
    """Real Spanner: a single-use snapshot permits exactly ONE read; the second
    raises. Prod bug fa9f5d4 was a single-use snapshot that grew a second read.
    The fake must fault so CI catches it, not prod."""
    db = _db()
    with db.snapshot() as snap:
        snap.execute_sql("SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@pk",
                         params={"pk": "ws_x"})
        with pytest.raises(ValueError, match="single-use snapshot"):
            snap.execute_sql("SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@pk",
                             params={"pk": "ws_x"})


def test_multi_use_snapshot_allows_multiple_reads() -> None:
    db = _db()
    with db.snapshot(multi_use=True) as snap:
        for _ in range(3):
            snap.execute_sql("SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@pk",
                             params={"pk": "ws_x"})  # no raise


def test_paged_entity_range_scan_serializes_against_a_concurrent_commit() -> None:
    """A range read must join the read set, or a scan-then-DELETE acts on stale state.

    `list_open_credit_transfers` is a read-WRITE transaction: it pages the
    `credit_transfer_open` index and DELETEs the rows whose transfer is already
    resolved. A range read's answer depends on which rows are ABSENT as much as
    which are present, and per-row versions cannot express "nothing was there",
    so the scan records a per-KIND version — the same device the outbox guard
    reads already use.

    Without it the fake let that transaction commit even though a concurrent
    commit had changed the scanned range, while real Spanner would abort it.
    The recovery queue is exactly where that matters: the walk would delete
    against a range it never saw, and an escrowed transfer can be dropped from
    the queue that is the only thing still asking about it.
    """
    from tests.fakes.spanner import make_fake_store

    store, database = make_fake_store()
    store._write_entity("credit_transfer_open", "t-a", {"transfer_id": "t-a"})
    attempts: list[int] = []

    def scan(transaction: object) -> list[str]:
        attempts.append(1)
        rows = list(
            transaction.execute_sql(  # type: ignore[attr-defined]
                "SELECT id FROM tr_entities WHERE kind=@kind AND id>@after "
                "ORDER BY id LIMIT @limit",
                params={"kind": "credit_transfer_open", "after": "", "limit": 100},
                param_types={
                    "kind": store._param_types.STRING,
                    "after": store._param_types.STRING,
                    "limit": store._param_types.INT64,
                },
            )
        )
        if len(attempts) == 1:
            # A competing transaction commits INTO the range we just scanned,
            # after our read and before our commit.
            store._write_entity("credit_transfer_open", "t-b", {"transfer_id": "t-b"})
        return [str(row[0]) for row in rows]

    scanned = database.run_in_transaction(scan)

    # ANTI-VACUITY: if the scan never aborted, this asserts nothing about
    # serialization and would pass on the unfixed fake.
    assert len(attempts) >= 2, (
        "the range scan committed without revalidating — a concurrent commit "
        "changed the range and the transaction did not abort"
    )
    # The re-run observed committed state rather than acting on the stale range.
    assert scanned == ["t-a", "t-b"]


def test_reservation_created_at_is_the_commit_timestamp_and_the_last_touch() -> None:
    """tr_reservation.created_at is allow_commit_timestamp: the insert writes
    PENDING_COMMIT_TIMESTAMP() (a client reading ahead of TrueTime would be
    refused), the fake stores its COMMIT_TIMESTAMP sentinel, and a later
    statement on the table in the same transaction is refused, so the insert
    stays the transaction's last touch of it."""
    from tests.fakes.spanner import FakeFailedPrecondition, _ParamTypes, _SpannerModule
    from trusted_router.storage_gcp_counter_dml import insert_reservation, read_reservation

    db = _db()
    fields = {
        "reservation_id": "res-pct", "workspace_id": "ws", "key_hash": "kh", "ws_shard": 0, "credit_shard": 0,
        "key_shard": 0, "credit_reserved_micro": 0, "key_reserved_micro": 0, "hold_usage_type": "Credits",
        "authorization_id": "gwa-pct", "idempotency_scope": "scope-pct", "idempotency_fingerprint": "fp",
        "expires_at": None,
    }
    db.run_in_transaction(lambda tx: insert_reservation(tx, _ParamTypes, **fields))
    assert db.reservations["res-pct"]["created_at"] == _SpannerModule.COMMIT_TIMESTAMP

    def insert_then_read(tx: object) -> None:
        insert_reservation(tx, _ParamTypes, **{**fields, "reservation_id": "res-pct-2", "idempotency_scope": None})
        read_reservation(tx, _ParamTypes, "res-pct")

    with pytest.raises(FakeFailedPrecondition, match="pending commit timestamp"):
        db.run_in_transaction(insert_then_read)
    assert "res-pct-2" not in db.reservations
