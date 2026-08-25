"""Durable settle outbox — native-Spanner storage (docs/design/durable-settle-outbox.md).

A native `tr_settle_outbox` table, NOT the broadcast entity/upsert store: the
exactly-once guarantee needs INSERT-as-claim (raises ALREADY_EXISTS on a
duplicate PK) plus lease-fenced conditional-DML, the same primitives
`storage_gcp_counter_dml` uses for `tr_reservation`. This module mirrors the
broadcast durable-job STATE MACHINE (pending -> done/dead + lease + exponential
backoff + max_attempts) but keeps its own persistence.

Increment 1 (this module): the storage layer + a `has_intent` predicate for the
reaper guard. The reaper wiring, the enqueue-at-settle call, the drain worker,
and the frozen-cost finalize primitive land in later increments; nothing here is
called on the live settle/reaper path yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
from trusted_router.storage_models import AutoRefillOutboxRow, SettleOutboxRow

# Column order shared by INSERT and the row-tuple SELECTs (keep in sync with the
# DDL in scripts/deploy/migrate_typed_counters.sh).
OUTBOX_COLUMNS = [
    "authorization_id",
    "intent_kind",
    "settle_origin",
    "reservation_id",
    "actual_cost_micro",
    "selected_endpoint_id",
    "model_id",
    "selected_usage_type",
    "settle_body",
    "status",
    "attempts",
    "last_error",
    "next_attempt_at",
    "lease_owner",
    "leased_until",
    "created_at",
    "updated_at",
    "terminal_at",
]

AUTO_REFILL_COLUMNS = [
    "authorization_id",
    "auto_refill_workspace_id",
    "auto_refill_status",
    "auto_refill_attempts",
    "auto_refill_last_error",
    "auto_refill_next_attempt_at",
    "auto_refill_lease_owner",
    "auto_refill_leased_until",
    "auto_refill_enqueued_at",
    "auto_refill_updated_at",
    "auto_refill_terminal_at",
]
INSERT_COLUMNS = [*OUTBOX_COLUMNS, *AUTO_REFILL_COLUMNS[1:]]

# Statuses that must FREEZE the hold — the reaper may not free-release a
# reservation whose authorization still has an outbox row in one of these
# (pending = will be drained; dead = drain gave up, a human must resolve).
# `release_approved` is deliberately excluded: it is the human's explicit ok to
# let the reaper free the hold. `done` means the charge already applied.
GUARD_STATUSES = ("pending", "dead")
_GUARD_STATUS_SQL = ", ".join(f"'{status}'" for status in GUARD_STATUSES)

# The reaper-guard predicate. SINGLE SOURCE OF TRUTH for this SQL: it is
# executed on a snapshot by has_intent (advisory pre-scan), on a snapshot by
# the reaper's advisory skip, and INSIDE settle_atomic's read-write
# transaction (the real interlock, MF2). Guard statuses come from
# GUARD_STATUSES; update the tuple, not the SQL literals.
GUARD_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_settle_outbox WHERE authorization_id=@aid "  # noqa: S608
    f"AND status IN ({_GUARD_STATUS_SQL})"
)
_SIBLING_GUARD_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_settle_outbox WHERE authorization_id=@aid "  # noqa: S608
    "AND intent_kind != @kind "
    f"AND status IN ({_GUARD_STATUS_SQL})"
)

# Enqueue outcomes.
ENQ_INSERTED = "inserted"  # new pending row
ENQ_REFRESHED = "refreshed"  # existing pending row's frozen inputs updated
ENQ_EXISTS_TERMINAL = "terminal"  # existing done/dead/release_approved row — left as is
ENQ_LEASED = "leased"  # existing pending row is actively leased by a drain — deferred


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_after_seconds(seconds: int) -> str:
    return (
        (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _backoff_seconds(attempts: int) -> int:
    return min(60 * 60, 2 ** max(attempts - 1, 0))


def _row_from_tuple(values: Any) -> SettleOutboxRow:
    d = dict(zip(OUTBOX_COLUMNS, values, strict=True))
    return SettleOutboxRow(
        authorization_id=d["authorization_id"],
        intent_kind=d["intent_kind"],
        settle_origin=d["settle_origin"],
        actual_cost_micro=int(d["actual_cost_micro"]),
        reservation_id=d["reservation_id"],
        selected_endpoint_id=d["selected_endpoint_id"],
        model_id=d["model_id"],
        selected_usage_type=d["selected_usage_type"],
        settle_body=d["settle_body"],
        status=d["status"],
        attempts=int(d["attempts"] or 0),
        last_error=d["last_error"],
        next_attempt_at=_ts_str(d["next_attempt_at"]),
        lease_owner=d["lease_owner"],
        leased_until=_ts_str(d["leased_until"]),
        created_at=_ts_str(d["created_at"]) or "",
        updated_at=_ts_str(d["updated_at"]),
        terminal_at=_ts_str(d["terminal_at"]),
    )


def _auto_refill_row_from_tuple(values: Any) -> AutoRefillOutboxRow:
    d = dict(zip(AUTO_REFILL_COLUMNS, values, strict=True))
    return AutoRefillOutboxRow(
        authorization_id=d["authorization_id"],
        workspace_id=d["auto_refill_workspace_id"],
        status=d["auto_refill_status"],
        attempts=int(d["auto_refill_attempts"] or 0),
        last_error=d["auto_refill_last_error"],
        next_attempt_at=_ts_str(d["auto_refill_next_attempt_at"]),
        lease_owner=d["auto_refill_lease_owner"],
        leased_until=_ts_str(d["auto_refill_leased_until"]),
        enqueued_at=_ts_str(d["auto_refill_enqueued_at"]) or "",
        updated_at=_ts_str(d["auto_refill_updated_at"]),
        terminal_at=_ts_str(d["auto_refill_terminal_at"]),
    )


def _ts_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    # spanner returns datetime; normalize to the Z-suffixed iso the model uses
    return value.isoformat().replace("+00:00", "Z")


class SpannerSettleOutbox:
    """Durable settle-intent store on a native `tr_settle_outbox` table."""

    def __init__(self, database: Any, param_types: Any) -> None:
        self._database = database
        self._pt = param_types

    # ── enqueue (INSERT-as-claim, refresh-latest on a still-pending row) ──────
    def enqueue(self, row: SettleOutboxRow, *, initial_delay_seconds: int = 0) -> str:
        """Record a settle intent. Idempotent by (authorization_id, intent_kind):

        - no row yet -> INSERT a pending row (ENQ_INSERTED)
        - a PENDING row exists -> refresh its frozen inputs to this (latest)
          delivery (ENQ_REFRESHED) — the enclave may retry with corrected actuals
        - a terminal row exists (done/dead/release_approved) -> leave it
          (ENQ_EXISTS_TERMINAL); the charge is already resolved or frozen.
        """
        pt = self._pt
        now = _iso_now()
        next_attempt_at = (
            _iso_after_seconds(initial_delay_seconds) if initial_delay_seconds > 0 else now
        )

        def insert_txn(transaction: Any) -> None:
            cols = ", ".join(INSERT_COLUMNS)
            binds = ", ".join(f"@{c}" for c in INSERT_COLUMNS)
            auto_refill_requested = row.auto_refill_workspace_id is not None
            values = {
                "authorization_id": row.authorization_id,
                "intent_kind": row.intent_kind,
                "settle_origin": row.settle_origin,
                "reservation_id": row.reservation_id,
                "actual_cost_micro": int(row.actual_cost_micro),
                "selected_endpoint_id": row.selected_endpoint_id,
                "model_id": row.model_id,
                "selected_usage_type": row.selected_usage_type,
                "settle_body": row.settle_body,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "next_attempt_at": next_attempt_at,
                "lease_owner": None,
                "leased_until": None,
                "created_at": now,
                "updated_at": now,
                "terminal_at": None,
                "auto_refill_workspace_id": row.auto_refill_workspace_id,
                "auto_refill_status": "pending" if auto_refill_requested else None,
                "auto_refill_attempts": 0,
                "auto_refill_last_error": None,
                "auto_refill_next_attempt_at": (
                    next_attempt_at if auto_refill_requested else None
                ),
                "auto_refill_lease_owner": None,
                "auto_refill_leased_until": None,
                "auto_refill_enqueued_at": now if auto_refill_requested else None,
                "auto_refill_updated_at": now if auto_refill_requested else None,
                "auto_refill_terminal_at": None,
            }
            types = {
                "authorization_id": pt.STRING,
                "intent_kind": pt.STRING,
                "settle_origin": pt.STRING,
                "reservation_id": pt.STRING,
                "actual_cost_micro": pt.INT64,
                "selected_endpoint_id": pt.STRING,
                "model_id": pt.STRING,
                "selected_usage_type": pt.STRING,
                "settle_body": pt.STRING,
                "status": pt.STRING,
                "attempts": pt.INT64,
                "last_error": pt.STRING,
                "next_attempt_at": pt.TIMESTAMP,
                "lease_owner": pt.STRING,
                "leased_until": pt.TIMESTAMP,
                "created_at": pt.TIMESTAMP,
                "updated_at": pt.TIMESTAMP,
                "terminal_at": pt.TIMESTAMP,
                "auto_refill_workspace_id": pt.STRING,
                "auto_refill_status": pt.STRING,
                "auto_refill_attempts": pt.INT64,
                "auto_refill_last_error": pt.STRING,
                "auto_refill_next_attempt_at": pt.TIMESTAMP,
                "auto_refill_lease_owner": pt.STRING,
                "auto_refill_leased_until": pt.TIMESTAMP,
                "auto_refill_enqueued_at": pt.TIMESTAMP,
                "auto_refill_updated_at": pt.TIMESTAMP,
                "auto_refill_terminal_at": pt.TIMESTAMP,
            }
            transaction.execute_update(
                f"INSERT INTO tr_settle_outbox ({cols}) VALUES ({binds})",  # noqa: S608 - fixed column list
                params=values,
                param_types=types,
            )
            # An outbox intent is durable repair work: keep both referenced
            # records TTL-ineligible. This also re-disarms retention if a reaper
            # armed it immediately before this enqueue committed.
            self._defer_retention(transaction, row.authorization_id, row.reservation_id)

        try:
            run_in_transaction_with_retry(self._database, insert_txn)
            return ENQ_INSERTED
        except Exception as exc:  # ALREADY_EXISTS -> the intent is already recorded
            if not _is_already_exists(exc):
                raise

        # Refresh the frozen inputs iff the existing row is still pending AND not
        # actively leased. A claimed row stays status='pending' while a drain
        # worker applies it, so refreshing on status alone could overwrite
        # actual_cost_micro / body out from under an in-flight apply (codex #113
        # finding 2). The lease fence makes a retry-enqueue a no-op while a drain
        # holds the row; once the lease lapses (or the drain fails back to
        # pending) a later enqueue can refresh again.
        def refresh_txn(transaction: Any) -> int:
            refreshed = transaction.execute_update(
                "UPDATE tr_settle_outbox SET settle_origin=@settle_origin, "
                "reservation_id=@reservation_id, actual_cost_micro=@actual_cost_micro, "
                "selected_endpoint_id=@selected_endpoint_id, model_id=@model_id, "
                "selected_usage_type=@selected_usage_type, settle_body=@settle_body, "
                "auto_refill_workspace_id=COALESCE(auto_refill_workspace_id, "
                "@auto_refill_workspace_id), auto_refill_status=CASE WHEN "
                "auto_refill_status IS NULL AND @auto_refill_workspace_id IS NOT NULL "
                "THEN 'pending' ELSE auto_refill_status END, "
                "auto_refill_next_attempt_at=CASE WHEN auto_refill_status IS NULL AND "
                "@auto_refill_workspace_id IS NOT NULL THEN @now ELSE "
                "auto_refill_next_attempt_at END, auto_refill_enqueued_at=CASE WHEN "
                "auto_refill_status IS NULL AND @auto_refill_workspace_id IS NOT NULL "
                "THEN @now ELSE auto_refill_enqueued_at END, "
                "auto_refill_updated_at=CASE WHEN auto_refill_status IS NULL AND "
                "@auto_refill_workspace_id IS NOT NULL THEN @now ELSE "
                "auto_refill_updated_at END, "
                "updated_at=@now WHERE authorization_id=@authorization_id "
                "AND intent_kind=@intent_kind AND status='pending' "
                "AND (leased_until IS NULL OR leased_until < @now)",
                params={
                    "settle_origin": row.settle_origin,
                    "reservation_id": row.reservation_id,
                    "actual_cost_micro": int(row.actual_cost_micro),
                    "selected_endpoint_id": row.selected_endpoint_id,
                    "model_id": row.model_id,
                    "selected_usage_type": row.selected_usage_type,
                    "settle_body": row.settle_body,
                    "auto_refill_workspace_id": row.auto_refill_workspace_id,
                    "now": now,
                    "authorization_id": row.authorization_id,
                    "intent_kind": row.intent_kind,
                },
                param_types={
                    "settle_origin": pt.STRING,
                    "reservation_id": pt.STRING,
                    "actual_cost_micro": pt.INT64,
                    "selected_endpoint_id": pt.STRING,
                    "model_id": pt.STRING,
                    "selected_usage_type": pt.STRING,
                    "settle_body": pt.STRING,
                    "auto_refill_workspace_id": pt.STRING,
                    "now": pt.TIMESTAMP,
                    "authorization_id": pt.STRING,
                    "intent_kind": pt.STRING,
                },
            )
            if refreshed == 1:
                # Refresh is an outstanding intent too, so its referenced
                # records must remain ineligible for retention TTL.
                self._defer_retention(transaction, row.authorization_id, row.reservation_id)
            return refreshed

        refreshed = run_in_transaction_with_retry(self._database, refresh_txn)
        if refreshed == 1:
            return ENQ_REFRESHED
        # 0-row: classify for accurate observability (codex #113) — a still-pending
        # row means the refresh was fenced out by an active lease (a drain holds
        # it), distinct from a genuinely terminal (done/dead/release_approved) row.
        existing = self.get(row.authorization_id, row.intent_kind)
        if existing is not None and existing.status == "pending":
            return ENQ_LEASED
        return ENQ_EXISTS_TERMINAL

    # ── due / claim / mark ───────────────────────────────────────────────────
    def due(self, *, limit: int = 100) -> list[SettleOutboxRow]:
        now = _iso_now()
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    f"SELECT {', '.join(OUTBOX_COLUMNS)} "  # noqa: S608 - fixed column list
                    "FROM tr_settle_outbox"
                    "@{FORCE_INDEX=tr_settle_outbox_due_v2} "
                    "WHERE queue_shard IS NOT NULL "
                    "AND next_attempt_at IS NOT NULL "
                    "AND status='pending' AND next_attempt_at <= @now "
                    "ORDER BY next_attempt_at LIMIT @limit",
                    params={"now": now, "limit": int(limit)},
                    param_types={"now": self._pt.TIMESTAMP, "limit": self._pt.INT64},
                )
            )
        return [_row_from_tuple(r) for r in rows]

    def claim(self, *, limit: int = 100, lease_seconds: int = 60) -> list[SettleOutboxRow]:
        owner = f"soworker_{uuid.uuid4().hex}"
        lease_until = _iso_after_seconds(lease_seconds)
        claimed: list[SettleOutboxRow] = []
        for candidate in self.due(limit=limit * 2):
            if len(claimed) >= limit:
                break
            if self._claim_one(candidate, owner=owner, lease_until=lease_until):
                candidate.lease_owner = owner
                candidate.leased_until = lease_until
                claimed.append(candidate)
        return claimed

    def _claim_one(self, row: SettleOutboxRow, *, owner: str, lease_until: str) -> bool:
        now = _iso_now()

        def txn(transaction: Any) -> int:
            return transaction.execute_update(
                "UPDATE tr_settle_outbox SET lease_owner=@owner, leased_until=@lease, "
                "updated_at=@now WHERE authorization_id=@aid AND intent_kind=@kind "
                "AND status='pending' AND (leased_until IS NULL OR leased_until < @now)",
                params={
                    "owner": owner,
                    "lease": lease_until,
                    "now": now,
                    "aid": row.authorization_id,
                    "kind": row.intent_kind,
                },
                param_types={
                    "owner": self._pt.STRING,
                    "lease": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP,
                    "aid": self._pt.STRING,
                    "kind": self._pt.STRING,
                },
            )

        return run_in_transaction_with_retry(self._database, txn) == 1

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
                sibling_rows = list(
                    transaction.execute_sql(
                        _SIBLING_GUARD_COUNT_SQL,
                        params={"aid": authorization_id, "kind": intent_kind},
                        param_types={
                            "aid": self._pt.STRING,
                            "kind": self._pt.STRING,
                        },
                    )
                )
                outstanding_siblings = int(sibling_rows[0][0]) if sibling_rows else 0
                # The PK is (authorization_id, intent_kind): settle and refund
                # coexist by design, so shared records must outlive the last
                # pending/dead intent, not merely the first one to finish.
                if outstanding_siblings == 0:
                    complete_gateway_authorization_retention(
                        transaction,
                        self._pt,
                        authorization_id,
                        terminal_at=now,
                        outbox_available=True,
                    )
                    if reservation_id:
                        complete_reservation_retention(
                            transaction,
                            self._pt,
                            str(reservation_id),
                            terminal_at=now,
                            outbox_available=True,
                        )
                else:
                    # Skipping the arm is not enough: a winning claim (or a
                    # rolling legacy finalize) may have ALREADY armed terminal_at
                    # after this row was enqueued, so the shared records would
                    # stay TTL-eligible while the sibling intent is outstanding.
                    self._defer_retention(transaction, authorization_id, reservation_id)
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

    def _defer_retention(
        self,
        transaction: Any,
        authorization_id: str,
        reservation_id: Any,
    ) -> None:
        """Keep both referenced records TTL-ineligible.

        THE INVARIANT: an outstanding (pending/dead) outbox intent always implies
        its reservation and gateway authorization are ineligible for the 30-day
        row-deletion policy — otherwise the frozen row outlives the very evidence
        its freeze exists to preserve. The three retention-arming DML sites now
        enforce that invariant structurally; these clears remain belt-and-braces
        defense-in-depth for already-armed state and every path that leaves or
        keeps an intent outstanding.
        """
        clear_gateway_authorization_retention(transaction, self._pt, authorization_id)
        if reservation_id:
            clear_reservation_retention(transaction, self._pt, str(reservation_id))

    def park(
        self,
        authorization_id: str,
        intent_kind: str,
        *,
        lease_owner: str | None,
        retry_after_seconds: int = 60,
        note: str = "typed store unavailable",
    ) -> bool:
        """Reschedule a row without burning attempts when typed storage is down."""
        now = _iso_now()
        next_at = _iso_after_seconds(retry_after_seconds)

        def txn(transaction: Any) -> bool:
            rows = list(
                transaction.execute_sql(
                    "SELECT attempts, lease_owner, reservation_id FROM tr_settle_outbox "
                    "WHERE authorization_id=@aid AND intent_kind=@kind AND status='pending'",
                    params={"aid": authorization_id, "kind": intent_kind},
                    param_types={"aid": self._pt.STRING, "kind": self._pt.STRING},
                )
            )
            if not rows:
                return False
            attempts, cur_owner = int(rows[0][0] or 0), rows[0][1]
            parked_reservation_id = rows[0][2]
            # Issue #355: anonymous callers may park only unleased rows, while
            # drain workers must still own the lease they are fencing with.
            if cur_owner != lease_owner:
                return False
            # §6: park != failure. A whole typed-backend outage must not walk
            # frozen rows toward dead; attempts stays unchanged and only the
            # schedule/error/lease fields move.
            updated = transaction.execute_update(
                "UPDATE tr_settle_outbox SET status='pending', last_error=@err, "
                "next_attempt_at=@next_at, lease_owner=NULL, leased_until=NULL, "
                "updated_at=@now WHERE authorization_id=@aid AND intent_kind=@kind "
                "AND status='pending' AND attempts=@attempts "
                "AND ((@lease_owner IS NULL AND lease_owner IS NULL) OR "
                "(@lease_owner IS NOT NULL AND lease_owner=@lease_owner))",
                params={
                    "attempts": attempts,
                    "err": note[:1000],
                    "next_at": next_at,
                    "now": now,
                    "aid": authorization_id,
                    "kind": intent_kind,
                    "lease_owner": lease_owner,
                },
                param_types={
                    "attempts": self._pt.INT64,
                    "err": self._pt.STRING,
                    "next_at": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP,
                    "aid": self._pt.STRING,
                    "kind": self._pt.STRING,
                    "lease_owner": self._pt.STRING,
                },
            )
            if updated != 1:
                return False
            # A parked row is still an outstanding intent, and park() is reached
            # AFTER a winning claim may have armed retention (e.g. the settle
            # committed but its activity index has not), so disarm here too.
            self._defer_retention(transaction, authorization_id, parked_reservation_id)
            return True

        return bool(run_in_transaction_with_retry(self._database, txn))

    # ── control-owned auto-refill sub-queue ─────────────────────────────────
    def attach_auto_refill(
        self,
        authorization_id: str,
        workspace_id: str,
        *,
        initial_delay_seconds: int = 0,
    ) -> bool:
        """Attach refill work independently of settlement status and leasing.

        This is a one-way NULL -> pending transition. It can therefore repair a
        pre-cutover row while a settle worker owns its lease, or after the
        settlement row became terminal, without mutating the frozen settlement.
        """
        now = _iso_now()
        next_attempt_at = (
            _iso_after_seconds(initial_delay_seconds) if initial_delay_seconds > 0 else now
        )

        def txn(transaction: Any) -> int:
            return transaction.execute_update(
                "UPDATE tr_settle_outbox SET auto_refill_workspace_id=@workspace_id, "
                "auto_refill_status='pending', auto_refill_attempts=0, "
                "auto_refill_last_error=NULL, auto_refill_next_attempt_at=@next_at, "
                "auto_refill_lease_owner=NULL, auto_refill_leased_until=NULL, "
                "auto_refill_enqueued_at=@now, auto_refill_updated_at=@now, "
                "auto_refill_terminal_at=NULL WHERE authorization_id=@aid "
                "AND intent_kind='settle' AND auto_refill_status IS NULL",
                params={
                    "workspace_id": workspace_id,
                    "next_at": next_attempt_at,
                    "now": now,
                    "aid": authorization_id,
                },
                param_types={
                    "workspace_id": self._pt.STRING,
                    "next_at": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP,
                    "aid": self._pt.STRING,
                },
            )

        if run_in_transaction_with_retry(self._database, txn) == 1:
            return True
        existing = self.get_auto_refill(authorization_id)
        return existing is not None and existing.workspace_id == workspace_id

    def due_auto_refills(self, *, limit: int = 100) -> list[AutoRefillOutboxRow]:
        now = _iso_now()
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    f"SELECT {', '.join(AUTO_REFILL_COLUMNS)} "  # noqa: S608 - fixed columns
                    "FROM tr_settle_outbox"
                    "@{FORCE_INDEX=tr_settle_outbox_auto_refill_due} "
                    "WHERE queue_shard IS NOT NULL "
                    "AND auto_refill_next_attempt_at IS NOT NULL "
                    "AND auto_refill_status='pending' "
                    "AND auto_refill_next_attempt_at <= @now "
                    "ORDER BY auto_refill_next_attempt_at LIMIT @limit",
                    params={"now": now, "limit": int(limit)},
                    param_types={"now": self._pt.TIMESTAMP, "limit": self._pt.INT64},
                )
            )
        return [_auto_refill_row_from_tuple(row) for row in rows]

    def claim_auto_refills(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[AutoRefillOutboxRow]:
        owner = f"arworker_{uuid.uuid4().hex}"
        lease_until = _iso_after_seconds(lease_seconds)
        claimed: list[AutoRefillOutboxRow] = []
        for candidate in self.due_auto_refills(limit=limit * 2):
            if len(claimed) >= limit:
                break
            if self._claim_auto_refill_one(
                candidate.authorization_id,
                owner=owner,
                lease_until=lease_until,
            ):
                candidate.lease_owner = owner
                candidate.leased_until = lease_until
                claimed.append(candidate)
        return claimed

    def _claim_auto_refill_one(
        self,
        authorization_id: str,
        *,
        owner: str,
        lease_until: str,
    ) -> bool:
        now = _iso_now()

        def txn(transaction: Any) -> int:
            return transaction.execute_update(
                "UPDATE tr_settle_outbox SET auto_refill_lease_owner=@owner, "
                "auto_refill_leased_until=@lease, auto_refill_updated_at=@now "
                "WHERE authorization_id=@aid AND intent_kind='settle' "
                "AND auto_refill_status='pending' AND "
                "(auto_refill_leased_until IS NULL OR auto_refill_leased_until < @now)",
                params={
                    "owner": owner,
                    "lease": lease_until,
                    "now": now,
                    "aid": authorization_id,
                },
                param_types={
                    "owner": self._pt.STRING,
                    "lease": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP,
                    "aid": self._pt.STRING,
                },
            )

        return run_in_transaction_with_retry(self._database, txn) == 1

    def resolve_auto_refill(
        self,
        authorization_id: str,
        *,
        lease_owner: str,
        done: bool,
        error: str | None = None,
        retry_after_seconds: int = 60,
        count_attempt: bool = True,
        max_attempts: int = 12,
    ) -> str | None:
        """Lease-fenced terminal or retry transition for refill work."""
        now = _iso_now()

        def txn(transaction: Any) -> str | None:
            rows = list(
                transaction.execute_sql(
                    "SELECT auto_refill_attempts, auto_refill_lease_owner "
                    "FROM tr_settle_outbox WHERE authorization_id=@aid "
                    "AND intent_kind='settle' AND auto_refill_status='pending'",
                    params={"aid": authorization_id},
                    param_types={"aid": self._pt.STRING},
                )
            )
            if not rows:
                return None
            attempts = int(rows[0][0] or 0)
            if rows[0][1] != lease_owner:
                return None
            next_attempts = attempts + (1 if count_attempt else 0)
            if done:
                status, next_at, terminal_at, note = "done", None, now, None
            elif next_attempts >= max_attempts:
                status, next_at, terminal_at = "dead", None, None
                note = (error or "auto-refill drain failed")[:1000]
            else:
                status = "pending"
                next_at = _iso_after_seconds(max(1, retry_after_seconds))
                terminal_at = None
                note = (error or "auto-refill retry")[:1000]
            updated = transaction.execute_update(
                "UPDATE tr_settle_outbox SET auto_refill_status=@status, "
                "auto_refill_attempts=@attempts, auto_refill_last_error=@error, "
                "auto_refill_next_attempt_at=@next_at, auto_refill_lease_owner=NULL, "
                "auto_refill_leased_until=NULL, auto_refill_updated_at=@now, "
                "auto_refill_terminal_at=@terminal_at WHERE authorization_id=@aid "
                "AND intent_kind='settle' AND auto_refill_status='pending' "
                "AND auto_refill_lease_owner=@lease_owner",
                params={
                    "status": status,
                    "attempts": next_attempts,
                    "error": note,
                    "next_at": next_at,
                    "now": now,
                    "terminal_at": terminal_at,
                    "aid": authorization_id,
                    "lease_owner": lease_owner,
                },
                param_types={
                    "status": self._pt.STRING,
                    "attempts": self._pt.INT64,
                    "error": self._pt.STRING,
                    "next_at": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP,
                    "terminal_at": self._pt.TIMESTAMP,
                    "aid": self._pt.STRING,
                    "lease_owner": self._pt.STRING,
                },
            )
            return status if updated == 1 else None

        return run_in_transaction_with_retry(self._database, txn)

    def get_auto_refill(self, authorization_id: str) -> AutoRefillOutboxRow | None:
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    f"SELECT {', '.join(AUTO_REFILL_COLUMNS)} "  # noqa: S608 - fixed columns
                    "FROM tr_settle_outbox WHERE authorization_id=@aid "
                    "AND intent_kind='settle' AND auto_refill_status IS NOT NULL",
                    params={"aid": authorization_id},
                    param_types={"aid": self._pt.STRING},
                )
            )
        return _auto_refill_row_from_tuple(rows[0]) if rows else None

    def auto_refill_pending_freshness(self) -> tuple[str | None, int]:
        """Return oldest pending enqueue timestamp and queue depth."""
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    "SELECT MIN(auto_refill_enqueued_at), COUNT(*) "
                    "FROM tr_settle_outbox WHERE auto_refill_status='pending'"
                )
            )
        if not rows:
            return None, 0
        return _ts_str(rows[0][0]), int(rows[0][1] or 0)

    # ── reaper guard predicate ───────────────────────────────────────────────
    def has_intent(self, authorization_id: str) -> bool:
        """True iff this authorization has an outbox row that FREEZES the hold
        (status in GUARD_STATUSES). The reaper must not free-release such a
        reservation. Read on a snapshot for the advisory pre-scan; the reaper
        also re-checks in-transaction (Increment 2) for the real interlock."""
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    GUARD_COUNT_SQL,
                    params={"aid": authorization_id},
                    param_types={"aid": self._pt.STRING},
                )
            )
        return bool(rows) and int(rows[0][0]) > 0

    def get(self, authorization_id: str, intent_kind: str) -> SettleOutboxRow | None:
        with self._database.snapshot() as snapshot:
            rows = list(
                snapshot.execute_sql(
                    f"SELECT {', '.join(OUTBOX_COLUMNS)} FROM tr_settle_outbox "  # noqa: S608 - fixed column list
                    "WHERE authorization_id=@aid AND intent_kind=@kind",
                    params={"aid": authorization_id, "kind": intent_kind},
                    param_types={"aid": self._pt.STRING, "kind": self._pt.STRING},
                )
            )
        return _row_from_tuple(rows[0]) if rows else None

    def purge_done(self, *, older_than_days: int = 30) -> int:
        """Compatibility no-op; Spanner TTL owns bounded terminal cleanup.

        Existing production rows intentionally retain ``terminal_at=NULL`` and
        are not deleted by this rollout. The argument remains to avoid breaking
        older drain callers during the rolling deployment.
        """
        _ = older_than_days
        return 0


def _is_already_exists(exc: Exception) -> bool:
    # Name-based check first (covers the test fake's FakeAlreadyExists too), then
    # the real type when the google libs are importable.
    if type(exc).__name__ in ("AlreadyExists", "FakeAlreadyExists"):
        return True
    try:
        from google.api_core.exceptions import AlreadyExists
    except Exception:  # pragma: no cover - google libs always present in prod/tests
        return False
    return isinstance(exc, AlreadyExists)
