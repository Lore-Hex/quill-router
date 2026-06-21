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
            return _replay(existing)

        try:
            return run_in_transaction_with_retry(database, replay_txn)
        except _Reject as reject:
            return {"outcome": reject.outcome}
