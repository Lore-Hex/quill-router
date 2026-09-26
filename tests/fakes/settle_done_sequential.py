"""Frozen sequential oracle from da86cb5d (#1340); do not use new SQL builders."""
from __future__ import annotations

from typing import Any

from trusted_router.storage_gcp_counter_dml import (
    clear_reservation_retention,
    complete_reservation_retention,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_request_records import (
    clear_gateway_authorization_retention,
    complete_gateway_authorization_retention,
)
from trusted_router.storage_gcp_settle_outbox import (
    _GUARD_STATUS_SQL,
    SpannerSettleOutbox,
    _backoff_seconds,
    _iso_after_seconds,
    _iso_now,
)

_SIBLING_GUARD_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_settle_outbox WHERE authorization_id=@aid "  # noqa: S608
    "AND intent_kind != @kind "
    f"AND status IN ({_GUARD_STATUS_SQL})"
)

def _defer_retention_tx(
    transaction: Any, param_types: Any, authorization_id: str, reservation_id: Any
) -> None:
    """Keep both referenced records TTL-ineligible (see _defer_retention)."""
    clear_gateway_authorization_retention(transaction, param_types, authorization_id)
    if reservation_id:
        clear_reservation_retention(transaction, param_types, str(reservation_id))

def _resolve_done_retention_tx(
    transaction: Any,
    param_types: Any,
    *,
    authorization_id: str,
    intent_kind: str,
    reservation_id: Any,
    now: str,
) -> None:
    """After an intent row goes ``done``: arm or defer retention on the shared
    authorization and reservation records, in the SAME transaction.

    The PK is (authorization_id, intent_kind): settle and refund coexist by
    design, so shared records must outlive the last pending/dead intent, not
    merely the first one to finish. Skipping the arm is not enough when a
    sibling is outstanding: a winning claim (or a rolling legacy finalize) may
    have ALREADY armed terminal_at after this row was enqueued, so the shared
    records would stay TTL-eligible while the sibling intent is outstanding.

    Shared by ``SpannerSettleOutbox.mark`` (the standalone commit) and
    ``mark_done_unleased_tx`` (inside the finalize commit) so the two cannot
    drift: the fold moved the mark, it must not move the retention contract.
    """
    sibling_rows = list(
        transaction.execute_sql(
            _SIBLING_GUARD_COUNT_SQL,
            params={"aid": authorization_id, "kind": intent_kind},
            param_types={"aid": param_types.STRING, "kind": param_types.STRING},
        )
    )
    outstanding_siblings = int(sibling_rows[0][0]) if sibling_rows else 0
    if outstanding_siblings == 0:
        complete_gateway_authorization_retention(
            transaction,
            param_types,
            authorization_id,
            terminal_at=now,
            outbox_available=True,
        )
        if reservation_id:
            complete_reservation_retention(
                transaction,
                param_types,
                str(reservation_id),
                terminal_at=now,
                outbox_available=True,
            )
    else:
        _defer_retention_tx(transaction, param_types, authorization_id, reservation_id)

_PENDING_ROW_SQL = (
    "SELECT attempts, lease_owner, reservation_id FROM tr_settle_outbox "
    "WHERE authorization_id=@aid AND intent_kind=@kind AND status='pending'"
)

_RESOLVE_ROW_SQL = (
    "UPDATE tr_settle_outbox SET status=@status, attempts=@attempts, "
    "last_error=@err, next_attempt_at=@next_at, lease_owner=NULL, "
    "leased_until=NULL, updated_at=@now, terminal_at=@terminal_at, "
    "settle_body=IF(@done, CAST(NULL AS STRING), settle_body) "
    "WHERE authorization_id=@aid "
    "AND intent_kind=@kind AND status='pending' "
    "AND ((@lease_owner IS NULL AND lease_owner IS NULL) OR "
    "(@lease_owner IS NOT NULL AND lease_owner=@lease_owner))"
)

def mark_done_unleased_tx(
    transaction: Any,
    param_types: Any,
    *,
    authorization_id: str,
    intent_kind: str,
) -> bool:
    """Resolve the pending intent row to ``done`` INSIDE a caller's transaction.

    Same lease fence as the inline path's ``mark(done=True)``: only an UNLEASED
    ``pending`` row is touched. A row a drain worker currently owns is left
    alone (returns False) and the drain re-derives ``done`` from the finalize
    outcome, exactly as when the standalone mark was skipped.

    Called from ``typed_finalize_atomic`` so the charge and the done-mark
    commit together. Before this the mark was a separate commit after
    finalize: one more multi-region round trip per settle, and a window in
    which a crash left an already-charged authorization ``pending`` -- the
    residual that docs/design/durable-settle-outbox.md §7 accepted and said
    to close this way once the outbox shared the Spanner instance.
    """
    rows = list(
        transaction.execute_sql(
            _PENDING_ROW_SQL,
            params={"aid": authorization_id, "kind": intent_kind},
            param_types={"aid": param_types.STRING, "kind": param_types.STRING},
        )
    )
    if not rows:
        return False
    attempts, cur_owner, reservation_id = int(rows[0][0] or 0), rows[0][1], rows[0][2]
    if cur_owner is not None:
        return False
    now = _iso_now()
    updated = transaction.execute_update(
        _RESOLVE_ROW_SQL,
        params={
            "status": "done",
            "attempts": attempts + 1,
            "err": None,
            "next_at": None,
            "now": now,
            "terminal_at": now,
            "done": True,
            "aid": authorization_id,
            "kind": intent_kind,
            "lease_owner": None,
        },
        param_types={
            "status": param_types.STRING,
            "attempts": param_types.INT64,
            "err": param_types.STRING,
            "next_at": param_types.TIMESTAMP,
            "now": param_types.TIMESTAMP,
            "aid": param_types.STRING,
            "kind": param_types.STRING,
            "lease_owner": param_types.STRING,
            "terminal_at": param_types.TIMESTAMP,
            "done": param_types.BOOL,
        },
    )
    if int(updated) != 1:
        return False
    # The mark moved into the finalize commit; the retention contract that
    # rode along with it (arm terminal_at on the shared records, or defer it
    # while a sibling intent is outstanding) moves with it.
    _resolve_done_retention_tx(
        transaction,
        param_types,
        authorization_id=authorization_id,
        intent_kind=intent_kind,
        reservation_id=reservation_id,
        now=now,
    )
    return True

class SequentialSpannerSettleOutbox(SpannerSettleOutbox):
    def mark(
        self,
        authorization_id: str,
        intent_kind: str,
        *,
        done: bool,
        error: str | None = None,
        lease_owner: str | None = None,
        max_attempts: int = 8,
        force_dead: bool = False,
    ) -> str | None:
        """Resolve a drained row in ONE lease-fenced conditional-DML transaction.

        `done=True` -> status='done' (terminal). `done=False` -> back off to
        'pending' with the next attempt time, or 'dead' at max_attempts (which
        FREEZES the hold for a human — see GUARD_STATUSES). With
        `done=False, force_dead=True`, the row goes straight to `dead` while
        still incrementing attempts for the audit trail. Dead FREEZES the hold
        (GUARD_STATUSES) until a human sets `release_approved`. Returns the new
        status, or None if the row was not claimable by this owner (lost lease /
        already resolved). A worker that lost its lease cannot resolve the row;
        the winner (or next claimant) re-runs the idempotent apply to re-derive
        the outcome. Only 'pending' rows are marked."""
        now = _iso_now()

        def txn(transaction: Any) -> str | None:
            rows = list(
                transaction.execute_sql(
                    "SELECT attempts, lease_owner, reservation_id FROM tr_settle_outbox "
                    "WHERE authorization_id=@aid AND intent_kind=@kind AND status='pending'",
                    params={"aid": authorization_id, "kind": intent_kind},
                    param_types={"aid": self._pt.STRING, "kind": self._pt.STRING},
                )
            )
            if not rows:
                return None
            attempts, cur_owner, reservation_id = (
                int(rows[0][0] or 0),
                rows[0][1],
                rows[0][2],
            )
            # Issue #355: anonymous inline callers may touch only unleased rows,
            # while drain workers may touch only rows they still own.
            if cur_owner != lease_owner:
                return None
            next_attempts = attempts + 1
            if done:
                new_status, next_at, err, terminal_at = "done", None, None, now
            elif force_dead:
                new_status, next_at, err, terminal_at = (
                    "dead",
                    None,
                    (error or "drain failed")[:1000],
                    None,
                )
            elif next_attempts >= max_attempts:
                new_status, next_at, err, terminal_at = (
                    "dead",
                    None,
                    (error or "drain failed")[:1000],
                    None,
                )
            else:
                new_status = "pending"
                next_at = _iso_after_seconds(_backoff_seconds(next_attempts))
                err = (error or "drain failed")[:1000]
                terminal_at = None
            updated = transaction.execute_update(
                "UPDATE tr_settle_outbox SET status=@status, attempts=@attempts, "
                "last_error=@err, next_attempt_at=@next_at, lease_owner=NULL, "
                "leased_until=NULL, updated_at=@now, terminal_at=@terminal_at, "
                "settle_body=IF(@done, CAST(NULL AS STRING), settle_body) "
                "WHERE authorization_id=@aid "
                "AND intent_kind=@kind AND status='pending' "
                "AND ((@lease_owner IS NULL AND lease_owner IS NULL) OR "
                "(@lease_owner IS NOT NULL AND lease_owner=@lease_owner))",
                params={
                    "status": new_status,
                    "attempts": next_attempts,
                    "err": err,
                    "next_at": next_at,
                    "now": now,
                    "terminal_at": terminal_at,
                    "done": done,
                    "aid": authorization_id,
                    "kind": intent_kind,
                    "lease_owner": lease_owner,
                },
                param_types={
                    "status": self._pt.STRING,
                    "attempts": self._pt.INT64,
                    "err": self._pt.STRING,
                    "next_at": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP,
                    "aid": self._pt.STRING,
                    "kind": self._pt.STRING,
                    "lease_owner": self._pt.STRING,
                    "terminal_at": self._pt.TIMESTAMP,
                    "done": self._pt.BOOL,
                },
            )
            if updated != 1:
                return None
            if done:
                _resolve_done_retention_tx(
                    transaction,
                    self._pt,
                    authorization_id=authorization_id,
                    intent_kind=intent_kind,
                    reservation_id=reservation_id,
                    now=now,
                )
            else:
                # Non-terminal outcome (backoff to pending, or dead awaiting a
                # human): repair work is still outstanding, so the referenced
                # records must stay TTL-ineligible. This also disarms retention
                # that a WINNING claim armed earlier — settle_atomic sets
                # terminal_at on the reservation at claim time, so a row that
                # later goes dead would otherwise keep a 30-day fuse on the very
                # records its freeze exists to preserve.
                self._defer_retention(transaction, authorization_id, reservation_id)
            return new_status

        return run_in_transaction_with_retry(self._database, txn)
