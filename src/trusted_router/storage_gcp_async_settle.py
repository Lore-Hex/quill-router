"""Async admission commits responsibility, never money. No catalog/credit/key IO."""
from __future__ import annotations

import time
from typing import Any

from google.api_core.exceptions import DeadlineExceeded, FailedPrecondition

from trusted_router.storage_gcp_authorize import _dispose_open_transaction
from trusted_router.storage_gcp_batch_dml import DmlStatement, execute_batch_dml
from trusted_router.storage_gcp_io import (
    run_in_transaction_with_retry,
    spanner_rpc_deadline,
)
from trusted_router.storage_gcp_settle_outbox import (
    SpannerSettleOutbox,
    _iso_now,
    intent_insert_counts,
    intent_insert_statements,
)
from trusted_router.storage_models import SettleOutboxRow


class ReservationNotOpen(FailedPrecondition):
    """Definitive admission miss, raised inside the transaction before commit."""


def async_reservation_admission_statement(pt: Any, row: SettleOutboxRow) -> DmlStatement:
    return (
        "UPDATE tr_reservation SET terminal_at=NULL "
        "WHERE reservation_id=@rid AND authorization_id=@aid AND settled=false",
        {"rid": row.reservation_id, "aid": row.authorization_id},
        {"rid": pt.STRING, "aid": pt.STRING},
    )


def enqueue(outbox: SpannerSettleOutbox, row: SettleOutboxRow, deadline: float) -> None:
    """One INSERT and retention batch. Caller resolves duplicates after rollback.

    Metadata is in the same INSERT, so workspace sparse-index membership cannot
    lag acceptance. Generated due columns are left to Spanner.
    """
    pt = outbox._pt
    now = _iso_now()
    row.created_at = now
    row.updated_at = now
    row.next_attempt_at = now
    statements = intent_insert_statements(pt, row, now=now, next_attempt_at=now, resolved=False)
    sql, params, types = statements[0]
    extra = {"async_version": 1, "workspace_id": row.workspace_id,
             "snapshot_hash": row.snapshot_hash, "payload_hash": row.payload_hash}
    sql = sql.replace(") VALUES (", ", " + ", ".join(extra) + ") VALUES (")
    sql = sql[:-1] + ", " + ", ".join("@" + name for name in extra) + ")"
    statements[0] = (sql, {**params, **extra}, {**types, "async_version": pt.INT64,
                    "workspace_id": pt.STRING, "snapshot_hash": pt.STRING, "payload_hash": pt.STRING})
    counts = [*intent_insert_counts(statements), (1,)]
    statements.append(async_reservation_admission_statement(pt, row))
    opened: list[Any] = []

    def transaction(tx: Any) -> None:
        opened[:] = [tx]

        def admission_count(actual: Any) -> None:
            if len(actual) == len(statements) and actual[-1] == 0:
                raise ReservationNotOpen("Reservation no longer open")

        execute_batch_dml(tx, statements, counts, check_prefix=admission_count)
        if time.monotonic() >= deadline:
            raise DeadlineExceeded("Async handoff budget exhausted before commit")

    with spanner_rpc_deadline(deadline):
        try:
            run_in_transaction_with_retry(
                outbox._database, transaction, total_budget_seconds=max(0, deadline-time.monotonic()),
                transaction_tag="tr_async_" + row.intent_kind + "_enqueue",
            )
            if time.monotonic() >= deadline:
                raise DeadlineExceeded("Async handoff outcome requires reconciliation")
        except BaseException:
            _dispose_open_transaction(opened)
            raise
