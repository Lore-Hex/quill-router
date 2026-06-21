"""Step 3b-3: the atomic gateway-authorize transaction (DML-only).

See docs/design/billing-typed-counters.md.

ONE Spanner read-write transaction (no mutation mixing) owns the whole authorize
decision, so a crash can never leak a hold (codex#1 #1):

  scoped idempotency read (+ fingerprint) ->
  conditional key-cap DML -> conditional credit DML ->
  tr_reservation INSERT (exact holds + hold usage type + authorization_id) ->
  gateway_authorization DML INSERT.

A rejection (insufficient credits / key cap) raises inside the callback, which
rolls the whole transaction back — releasing any hold already taken atomically,
no compensation needed. A duplicate idempotency_scope (concurrent first-call
loser) surfaces ALREADY_EXISTS (NOT retried); we re-read and REPLAY — no second
debit. Replay is resume/no-execute: the caller must NOT re-run the LLM call
(codex#2 #4).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from google.api_core.exceptions import AlreadyExists

from trusted_router.storage_gcp_counter_dml import (
    KEY_ACCEPTED,
    KEY_INSUFFICIENT,
    KEY_MISSING,
    insert_entity_dml,
    insert_reservation,
    read_reservation_by_idempotency,
    reserve_credit,
    reserve_key,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry


class AuthorizeOutcome:
    ACCEPTED = "accepted"
    REPLAY = "replay"  # idempotent replay: resume, do NOT re-execute
    INSUFFICIENT_CREDITS = "insufficient_credits"
    KEY_LIMIT_EXCEEDED = "key_limit_exceeded"
    KEY_MISSING = "key_missing"  # typed key row absent -> fail closed
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"  # same key, different request body


class _Reject(Exception):
    """Roll the authorize transaction back with a terminal outcome (not retried)."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


def authorize_atomic(
    database: Any,
    param_types: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int,
    has_credit_candidate: bool,
    reservation_usage_type: str,
    idempotency_scope: str | None,
    idempotency_fingerprint: str | None,
    expires_at: Any,
    build_auth_body: Callable[[str, str], str],
) -> dict:
    """Run the atomic authorize. Returns {outcome, reservation_id?, authorization_id?}.

    `build_auth_body(authorization_id, reservation_id) -> json str` lets the caller
    construct the gateway_authorization body once the ids are known.
    `reservation_usage_type` is the HOLD usage type (Credits if any credit
    candidate, else BYOK). `has_credit_candidate` gates the credit hold.
    """
    pt = param_types
    is_byok = not has_credit_candidate
    # Stable ids across ABORTED retries (only the committed attempt persists).
    reservation_id = str(uuid.uuid4())
    authorization_id = f"gwa-{uuid.uuid4().hex}"

    def _replay(existing: dict) -> dict:
        return {
            "outcome": AuthorizeOutcome.REPLAY,
            "reservation_id": existing["reservation_id"],
            "authorization_id": existing["authorization_id"],
        }

    def txn(transaction: Any) -> dict:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(transaction, pt, idempotency_scope)
            if existing is not None:
                if existing["idempotency_fingerprint"] != idempotency_fingerprint:
                    raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
                return _replay(existing)

        key_result = reserve_key(transaction, pt, key_hash, estimate, is_byok=is_byok)
        if key_result == KEY_INSUFFICIENT:
            raise _Reject(AuthorizeOutcome.KEY_LIMIT_EXCEEDED)
        if key_result == KEY_MISSING:
            raise _Reject(AuthorizeOutcome.KEY_MISSING)
        key_hold = estimate if key_result == KEY_ACCEPTED else 0

        credit_hold = 0
        if has_credit_candidate:
            if not reserve_credit(transaction, pt, workspace_id, estimate):
                raise _Reject(AuthorizeOutcome.INSUFFICIENT_CREDITS)
            credit_hold = estimate

        insert_reservation(
            transaction, pt,
            reservation_id=reservation_id, workspace_id=workspace_id, key_hash=key_hash,
            ws_shard=0, key_shard=0,
            credit_reserved_micro=credit_hold, key_reserved_micro=key_hold,
            hold_usage_type=reservation_usage_type, authorization_id=authorization_id,
            idempotency_scope=idempotency_scope, idempotency_fingerprint=idempotency_fingerprint,
            expires_at=expires_at,
        )
        insert_entity_dml(
            transaction, pt, "gateway_authorization", authorization_id,
            build_auth_body(authorization_id, reservation_id),
        )
        return {
            "outcome": AuthorizeOutcome.ACCEPTED,
            "reservation_id": reservation_id,
            "authorization_id": authorization_id,
        }

    try:
        return run_in_transaction_with_retry(database, txn)
    except _Reject as reject:
        return {"outcome": reject.outcome}
    except AlreadyExists:
        # Concurrent first-call lost the unique-idempotency-index race: the winner
        # committed; re-read and replay (codex Step-3 #4) — never a second debit.
        def replay_txn(transaction: Any) -> dict:
            existing = read_reservation_by_idempotency(transaction, pt, idempotency_scope)
            if existing is None:  # pragma: no cover - winner must exist post-conflict
                raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
            # Same fingerprint check as the normal replay path (codex keystone
            # review): a concurrent same-scope but DIFFERENT-body loser must get
            # IDEMPOTENCY_MISMATCH, not the winner's authorization as a replay.
            if existing["idempotency_fingerprint"] != idempotency_fingerprint:
                raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
            return _replay(existing)

        try:
            return run_in_transaction_with_retry(database, replay_txn)
        except _Reject as reject:
            return {"outcome": reject.outcome}


class SettleOutcome:
    SETTLED = "settled"  # this caller claimed + released the holds
    ALREADY_SETTLED = "already_settled"  # replay: another caller already settled
    NOT_FOUND = "not_found"  # no such reservation
    ERROR = "error"  # a release row-count != 1 -> rolled back, re-drive/alarm


class _SettleError(Exception):
    """A release returned row-count != 1 — roll the settle back (don't leave the
    reservation claimed with the hold unreleased / charge unbooked)."""


def settle_atomic(
    database: Any,
    param_types: Any,
    *,
    reservation_id: str,
    actual_micro: int,
    settled_usage_type: str,
    success: bool,
) -> dict:
    """Claim-gated settle/refund in ONE transaction (key then credit lock order).

    Claim flips settled false->true (first-writer-wins); only the winner releases
    the EXACT recorded holds and books `actual`. `success=False` is a refund:
    release the holds, book nothing. Booking matches the legacy finalize: key
    usage by settled usage type (usage vs byok_usage); credit total_usage only
    when the settled usage type is Credits.
    """
    from trusted_router.storage_gcp_counter_dml import (
        claim_reservation,
        read_reservation,
        release_credit,
        release_key,
    )

    pt = param_types
    book_actual = actual_micro if success else 0
    book_to_byok = settled_usage_type == "BYOK"

    def txn(transaction: Any) -> dict:
        res = read_reservation(transaction, pt, reservation_id)
        if res is None:
            return {"outcome": SettleOutcome.NOT_FOUND}
        won = claim_reservation(
            transaction, pt, reservation_id,
            actual_micro=book_actual, settled_usage_type=settled_usage_type,
        )
        if not won:
            return {"outcome": SettleOutcome.ALREADY_SETTLED}  # replay, no double-apply

        # key first, then credit (single lock order everywhere — codex#2 #2).
        key_actual = book_actual  # key usage counts under both Credits and BYOK
        key_count = release_key(
            transaction, pt, res["key_hash"], res["key_reserved_micro"],
            key_actual, book_to_byok=book_to_byok,
        )
        # A recorded hold MUST release; an uncapped/no-hold row (key_reserved==0)
        # may 0-row and is tolerated (best-effort usage tracking).
        if res["key_reserved_micro"] > 0 and key_count != 1:
            raise _SettleError("key release row-count != 1")

        if res["credit_reserved_micro"] > 0:
            credit_actual = book_actual if settled_usage_type == "Credits" else 0
            credit_count = release_credit(
                transaction, pt, res["workspace_id"], res["credit_reserved_micro"],
                credit_actual,
            )
            if credit_count != 1:
                raise _SettleError("credit release row-count != 1")
        return {"outcome": SettleOutcome.SETTLED}

    try:
        return run_in_transaction_with_retry(database, txn)
    except _SettleError:
        return {"outcome": SettleOutcome.ERROR}


def reap_expired_reservations(
    database: Any, param_types: Any, *, now: Any, limit: int = 100
) -> int:
    """Reclaim crashed-before-settle reservations (settled=false AND expires_at<now).

    Releases each stranded reservation's holds via the SAME claim-gated settle
    path (success=False = refund, books nothing), so a late settle racing the
    reaper is safe — whoever claims the row first wins, the other no-ops.

    `expires_at` is set at authorize to the EXECUTION DEADLINE (max stream
    duration + settle-retry window + margin), so a reaped reservation is genuinely
    abandoned; releasing without a charge is the bounded-loss accept (red-team P1).
    A durable settle outbox (gateway persists actuals on response) is the planned
    enhancement to recover the rare completed-but-settle-lost charge instead of
    releasing it free; until then, keep `expires_at` generous. Returns the count
    reaped.
    """
    pt = param_types
    with database.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT reservation_id FROM tr_reservation "
                "WHERE settled=false AND expires_at < @now LIMIT @limit",
                params={"now": now, "limit": int(limit)},
                param_types={"now": pt.TIMESTAMP, "limit": pt.INT64},
            )
        )
    reaped = 0
    for (reservation_id,) in rows:
        result = settle_atomic(
            database, pt, reservation_id=reservation_id, actual_micro=0,
            settled_usage_type="Credits", success=False,
        )
        if result["outcome"] == SettleOutcome.SETTLED:
            reaped += 1
    return reaped
