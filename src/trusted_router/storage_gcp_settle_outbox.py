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

from trusted_router.storage_models import SettleOutboxRow

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
]

# Statuses that must FREEZE the hold — the reaper may not free-release a
# reservation whose authorization still has an outbox row in one of these
# (pending = will be drained; dead = drain gave up, a human must resolve).
# `release_approved` is deliberately excluded: it is the human's explicit ok to
# let the reaper free the hold. `done` means the charge already applied.
GUARD_STATUSES = ("pending", "dead")

# The reaper-guard predicate. SINGLE SOURCE OF TRUTH for this SQL: it is
# executed on a snapshot by has_intent (advisory pre-scan), on a snapshot by
# the reaper's advisory skip, and INSIDE settle_atomic's read-write
# transaction (the real interlock, MF2). The fake asserts both predicates
# (`authorization_id=@aid`, `status IN ('pending', 'dead')`) — keep the
# literal exactly in sync with GUARD_STATUSES.
GUARD_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_settle_outbox WHERE authorization_id=@aid "
    "AND status IN ('pending', 'dead')"
)

# Enqueue outcomes.
ENQ_INSERTED = "inserted"          # new pending row
ENQ_REFRESHED = "refreshed"        # existing pending row's frozen inputs updated
ENQ_EXISTS_TERMINAL = "terminal"   # existing done/dead/release_approved row — left as is
ENQ_LEASED = "leased"              # existing pending row is actively leased by a drain — deferred


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
    def enqueue(self, row: SettleOutboxRow) -> str:
        """Record a settle intent. Idempotent by (authorization_id, intent_kind):

        - no row yet -> INSERT a pending row (ENQ_INSERTED)
        - a PENDING row exists -> refresh its frozen inputs to this (latest)
          delivery (ENQ_REFRESHED) — the enclave may retry with corrected actuals
        - a terminal row exists (done/dead/release_approved) -> leave it
          (ENQ_EXISTS_TERMINAL); the charge is already resolved or frozen.
        """
        pt = self._pt
        now = _iso_now()

        def insert_txn(transaction: Any) -> None:
            cols = ", ".join(OUTBOX_COLUMNS)
            binds = ", ".join(f"@{c}" for c in OUTBOX_COLUMNS)
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
                "next_attempt_at": now,
                "lease_owner": None,
                "leased_until": None,
                "created_at": now,
                "updated_at": now,
            }
            types = {
                "authorization_id": pt.STRING, "intent_kind": pt.STRING,
                "settle_origin": pt.STRING, "reservation_id": pt.STRING,
                "actual_cost_micro": pt.INT64, "selected_endpoint_id": pt.STRING,
                "model_id": pt.STRING, "selected_usage_type": pt.STRING,
                "settle_body": pt.STRING, "status": pt.STRING, "attempts": pt.INT64,
                "last_error": pt.STRING, "next_attempt_at": pt.TIMESTAMP,
                "lease_owner": pt.STRING, "leased_until": pt.TIMESTAMP,
                "created_at": pt.TIMESTAMP, "updated_at": pt.TIMESTAMP,
            }
            transaction.execute_update(
                f"INSERT INTO tr_settle_outbox ({cols}) VALUES ({binds})",  # noqa: S608 - fixed column list
                params=values, param_types=types,
            )

        try:
            self._database.run_in_transaction(insert_txn)
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
            return transaction.execute_update(
                "UPDATE tr_settle_outbox SET settle_origin=@settle_origin, "
                "reservation_id=@reservation_id, actual_cost_micro=@actual_cost_micro, "
                "selected_endpoint_id=@selected_endpoint_id, model_id=@model_id, "
                "selected_usage_type=@selected_usage_type, settle_body=@settle_body, "
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
                    "now": now,
                    "authorization_id": row.authorization_id,
                    "intent_kind": row.intent_kind,
                },
                param_types={
                    "settle_origin": pt.STRING, "reservation_id": pt.STRING,
                    "actual_cost_micro": pt.INT64, "selected_endpoint_id": pt.STRING,
                    "model_id": pt.STRING, "selected_usage_type": pt.STRING,
                    "settle_body": pt.STRING, "now": pt.TIMESTAMP,
                    "authorization_id": pt.STRING, "intent_kind": pt.STRING,
                },
            )

        refreshed = self._database.run_in_transaction(refresh_txn)
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
            rows = list(snapshot.execute_sql(
                f"SELECT {', '.join(OUTBOX_COLUMNS)} FROM tr_settle_outbox "  # noqa: S608 - fixed column list
                "WHERE status='pending' AND next_attempt_at <= @now "
                "ORDER BY next_attempt_at LIMIT @limit",
                params={"now": now, "limit": int(limit)},
                param_types={"now": self._pt.TIMESTAMP, "limit": self._pt.INT64},
            ))
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
                    "owner": owner, "lease": lease_until, "now": now,
                    "aid": row.authorization_id, "kind": row.intent_kind,
                },
                param_types={
                    "owner": self._pt.STRING, "lease": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP, "aid": self._pt.STRING,
                    "kind": self._pt.STRING,
                },
            )

        return self._database.run_in_transaction(txn) == 1

    def mark(
        self,
        authorization_id: str,
        intent_kind: str,
        *,
        done: bool,
        error: str | None = None,
        lease_owner: str | None = None,
        max_attempts: int = 8,
    ) -> str | None:
        """Resolve a drained row in ONE lease-fenced conditional-DML transaction.

        `done=True` -> status='done' (terminal). `done=False` -> back off to
        'pending' with the next attempt time, or 'dead' at max_attempts (which
        FREEZES the hold for a human — see GUARD_STATUSES). Returns the new
        status, or None if the row was not claimable by this owner (lost lease /
        already resolved). Only 'pending' rows are marked."""
        now = _iso_now()

        def txn(transaction: Any) -> str | None:
            rows = list(transaction.execute_sql(
                "SELECT attempts, lease_owner FROM tr_settle_outbox "
                "WHERE authorization_id=@aid AND intent_kind=@kind AND status='pending'",
                params={"aid": authorization_id, "kind": intent_kind},
                param_types={"aid": self._pt.STRING, "kind": self._pt.STRING},
            ))
            if not rows:
                return None
            attempts, cur_owner = int(rows[0][0] or 0), rows[0][1]
            if lease_owner is not None and cur_owner not in (None, lease_owner):
                return None  # lost the lease to another worker
            next_attempts = attempts + 1
            if done:
                new_status, next_at, err = "done", None, None
            elif next_attempts >= max_attempts:
                new_status, next_at, err = "dead", None, (error or "drain failed")[:1000]
            else:
                new_status = "pending"
                next_at = _iso_after_seconds(_backoff_seconds(next_attempts))
                err = (error or "drain failed")[:1000]
            updated = transaction.execute_update(
                "UPDATE tr_settle_outbox SET status=@status, attempts=@attempts, "
                "last_error=@err, next_attempt_at=@next_at, lease_owner=NULL, "
                "leased_until=NULL, updated_at=@now WHERE authorization_id=@aid "
                "AND intent_kind=@kind AND status='pending'",
                params={
                    "status": new_status, "attempts": next_attempts, "err": err,
                    "next_at": next_at, "now": now,
                    "aid": authorization_id, "kind": intent_kind,
                },
                param_types={
                    "status": self._pt.STRING, "attempts": self._pt.INT64,
                    "err": self._pt.STRING, "next_at": self._pt.TIMESTAMP,
                    "now": self._pt.TIMESTAMP, "aid": self._pt.STRING,
                    "kind": self._pt.STRING,
                },
            )
            return new_status if updated == 1 else None

        return self._database.run_in_transaction(txn)

    # ── reaper guard predicate ───────────────────────────────────────────────
    def has_intent(self, authorization_id: str) -> bool:
        """True iff this authorization has an outbox row that FREEZES the hold
        (status in GUARD_STATUSES). The reaper must not free-release such a
        reservation. Read on a snapshot for the advisory pre-scan; the reaper
        also re-checks in-transaction (Increment 2) for the real interlock."""
        with self._database.snapshot() as snapshot:
            rows = list(snapshot.execute_sql(
                GUARD_COUNT_SQL,
                params={"aid": authorization_id},
                param_types={"aid": self._pt.STRING},
            ))
        return bool(rows) and int(rows[0][0]) > 0

    def get(self, authorization_id: str, intent_kind: str) -> SettleOutboxRow | None:
        with self._database.snapshot() as snapshot:
            rows = list(snapshot.execute_sql(
                f"SELECT {', '.join(OUTBOX_COLUMNS)} FROM tr_settle_outbox "  # noqa: S608 - fixed column list
                "WHERE authorization_id=@aid AND intent_kind=@kind",
                params={"aid": authorization_id, "kind": intent_kind},
                param_types={"aid": self._pt.STRING, "kind": self._pt.STRING},
            ))
        return _row_from_tuple(rows[0]) if rows else None


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
