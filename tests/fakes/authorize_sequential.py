"""Frozen pre-6a authorize decision path from 4725046 (do not adapt to production)."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from google.api_core.exceptions import AlreadyExists

from trusted_router.spend_lease_admission import classify_receipt_replay
from trusted_router.spend_windows import utcnow
from trusted_router.storage_gcp_authorize import (
    MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION,
    AuthorizeOutcome,
    _Reject,
)
from trusted_router.storage_gcp_batch_dml import execute_batch_dml
from trusted_router.storage_gcp_counter_dml import (
    KEY_ACCEPTED,
    KEY_INSUFFICIENT,
    KEY_MISSING,
    KEY_NO_HOLD,
    entity_insert_statement,
    read_reservation_by_idempotency,
    reservation_insert_statement,
    reserve_credit,
)
from trusted_router.storage_gcp_counters import UNSHARDED
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_request_records import (
    gateway_authorization_insert_statement,
    read_gateway_authorization_admission_columns,
)
from trusted_router.storage_models import GatewayAuthorization


def reserve_key(
    transaction: Any,
    param_types: Any,
    key_hash: str,
    amount: int,
    *,
    is_byok: bool,
    shard: int = UNSHARDED,
) -> str:
    """Atomically reserve `amount` against the per-key spend cap.

    Single conditional UPDATE: it matches (and holds) only a capped key whose
    available headroom covers `amount`, AND only when the cap applies to this
    usage type (BYOK is skipped when the key excludes BYOK). On row-count 0 we
    classify with a point-read IN THE SAME TRANSACTION (codex#2 #3) so the 0 is
    not ambiguous: missing row vs uncapped vs BYOK-excluded vs truly insufficient.

    Returns one of KEY_ACCEPTED / KEY_NO_HOLD / KEY_INSUFFICIENT / KEY_MISSING.
    """
    sql = (
        "UPDATE tr_key_limit SET reserved = reserved + @est "
        "WHERE key_hash=@kh AND shard=@shard AND limit_micro IS NOT NULL "
        "AND (@is_byok = FALSE OR include_byok = TRUE) "
        "AND (limit_micro - usage - IF(include_byok, byok_usage, 0) - reserved) >= @est"
    )
    count = transaction.execute_update(
        sql,
        params={"est": int(amount), "kh": key_hash, "shard": shard, "is_byok": bool(is_byok)},
        param_types={
            "est": param_types.INT64,
            "kh": param_types.STRING,
            "shard": param_types.INT64,
            "is_byok": param_types.BOOL,
        },
    )
    if count == 1:
        return KEY_ACCEPTED
    rows = list(
        transaction.execute_sql(
            "SELECT limit_micro, include_byok FROM tr_key_limit "
            "WHERE key_hash=@kh AND shard=@shard",
            params={"kh": key_hash, "shard": shard},
            param_types={"kh": param_types.STRING, "shard": param_types.INT64},
        )
    )
    if not rows:
        return KEY_MISSING
    limit_micro, include_byok = rows[0][0], rows[0][1]
    if limit_micro is None:
        return KEY_NO_HOLD  # uncapped
    if is_byok and not include_byok:
        return KEY_NO_HOLD  # BYOK excluded from this key's cap
    return KEY_INSUFFICIENT


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
    build_authorization: Callable[[str, str], GatewayAuthorization] | None = None,
    build_auth_body: Callable[[str, str], str] | None = None,
    request_record_write_mode: str = "legacy",
    credit_shard: int = UNSHARDED,
    credit_shard_candidates: tuple[int, ...] | None = None,
    key_shard_candidates: tuple[int, ...] = (UNSHARDED,),
    skip_key_limit: bool = False,
    authorization_id: str | None = None,
    spend_lease_hook: Callable[[Any, int], dict[str, Any]] | None = None,
    build_authorization_for_lease: (
        Callable[[str, str, bool], GatewayAuthorization] | None
    ) = None,
    also_retry: tuple[type[BaseException], ...] = (),
    spend_lease_receipt_hash: str | None = None,
    credit_escrowed_by_spend_lease: bool = False,
    spend_lease_admission_replay_protection: bool = False,
    trust_settings: Any = None,
) -> dict:
    """Run the atomic authorize. Returns {outcome, reservation_id?, authorization_id?}.

    `request_record_write_mode="legacy"` preserves the generic tr_entities write
    during the expand rollout. `"typed"` writes the same authorization into the
    bounded typed table. The corresponding builder is required for the selected
    mode.
    `reservation_usage_type` is the HOLD usage type (Credits if any credit
    candidate, else BYOK). `has_credit_candidate` gates the credit hold.
    `credit_shard_candidates` is a bounded, pre-randomized order built outside
    the transaction so Spanner retries use the same order. The first shard with
    enough independent sub-budget is recorded durably on the reservation. The
    store wrapper applies `bounded_credit_shard_candidates`; direct callers must
    likewise pass no more than the hot-path limit.

    `skip_key_limit` is opt-in: the gateway's already-loaded ApiKey must have
    no lifetime cap, regardless of window caps checked by the caller. Omitted
    by older callers, it preserves the existing reserve SQL. The first candidate
    still receives settlement usage, with zero held, without an authorize read
    or write of tr_key_limit.

    Per-window key caps are checked by the CALLER via check_key_window_limits on
    a lock-free snapshot BEFORE this transaction — deliberately NOT in here: a
    shared read of tr_key_limit followed by reserve_key's conditional UPDATE on
    the same row would reintroduce the read-lock-upgrade surface this DML-only
    transaction exists to eliminate (codex #93 review).
    """
    pt = param_types
    if request_record_write_mode not in {"legacy", "typed"}:
        raise ValueError("request_record_write_mode must be 'legacy' or 'typed'")
    if request_record_write_mode == "typed" and build_authorization is None:
        raise ValueError("typed request records require build_authorization")
    if request_record_write_mode == "legacy" and build_auth_body is None:
        raise ValueError("legacy request records require build_auth_body")
    shard_candidates: tuple[int, ...]
    if credit_shard_candidates is None:
        shard_candidates = (credit_shard,)
    else:
        shard_candidates = tuple(credit_shard_candidates)
        if credit_shard != UNSHARDED:
            raise ValueError("pass credit_shard or credit_shard_candidates, not both")
    if not shard_candidates:
        raise ValueError("credit_shard_candidates must not be empty")
    if any(shard < 0 for shard in shard_candidates):
        raise ValueError("credit shards must be non-negative")
    if len(set(shard_candidates)) != len(shard_candidates):
        raise ValueError("credit_shard_candidates must be unique")
    if has_credit_candidate and len(shard_candidates) > MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION:
        raise ValueError("credit_shard_candidates exceeds the hot-path transaction limit")
    if not has_credit_candidate and shard_candidates != (UNSHARDED,):
        raise ValueError("BYOK-only authorization must use credit shard zero")
    if credit_escrowed_by_spend_lease and (
        not has_credit_candidate or spend_lease_hook is None
    ):
        raise ValueError("lease-escrowed credit requires a Credits route and spend-lease hook")
    key_candidates = tuple(key_shard_candidates)
    if not key_candidates:
        raise ValueError("key_shard_candidates must not be empty")
    if any(shard < 0 for shard in key_candidates):
        raise ValueError("key shards must be non-negative")
    if len(set(key_candidates)) != len(key_candidates):
        raise ValueError("key_shard_candidates must be unique")
    is_byok = not has_credit_candidate
    # Stable ids across ABORTED retries (only the committed attempt persists).
    reservation_id = str(uuid.uuid4())
    authorization_id = authorization_id or f"gwa-{uuid.uuid4().hex}"
    created_at = utcnow()
    authorization = (
        build_authorization(authorization_id, reservation_id)
        if build_authorization is not None
        else None
    )
    if authorization is not None:
        authorization.created_at = created_at.isoformat().replace("+00:00", "Z")
    legacy_auth_body = (
        build_auth_body(authorization_id, reservation_id) if build_auth_body is not None else None
    )

    def _replay(transaction: Any, existing: dict) -> dict:
        receipt_verdict = "ordinary"
        if spend_lease_admission_replay_protection:
            admission = read_gateway_authorization_admission_columns(
                transaction,
                pt,
                str(existing["authorization_id"]),
            )
            stored_receipt_hash = (
                str(admission["spend_lease_receipt_hash"])
                if admission is not None
                and admission["spend_lease_receipt_hash"] is not None
                else None
            )
            receipt_verdict = classify_receipt_replay(
                spend_lease_receipt_hash,
                stored_receipt_hash,
            )
        if receipt_verdict == "scope_conflict":
            raise _Reject(AuthorizeOutcome.ADMISSION_SCOPE_CONFLICT)
        if receipt_verdict == "ordinary" and (
            existing["idempotency_fingerprint"] != idempotency_fingerprint
        ):
            raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
        return {
            "outcome": AuthorizeOutcome.REPLAY,
            "reservation_id": existing["reservation_id"],
            "authorization_id": existing["authorization_id"],
            "credit_shard": int(existing.get("credit_shard", UNSHARDED)),
            "key_shard": int(existing.get("key_shard", UNSHARDED)),
        }

    def txn(transaction: Any) -> dict:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(transaction, pt, idempotency_scope)
            if existing is not None:
                return _replay(transaction, existing)

        # Reserve credit before key so both paths use the same counter lock order.
        # Reservation/authorization INSERTs below consume the selected shards and holds.
        credit_hold = 0
        selected_credit_shard = UNSHARDED
        if has_credit_candidate and not credit_escrowed_by_spend_lease:
            for candidate in shard_candidates:
                if reserve_credit(transaction, pt, workspace_id, estimate, shard=candidate):
                    selected_credit_shard = candidate
                    break
            else:
                raise _Reject(AuthorizeOutcome.INSUFFICIENT_CREDITS)
            credit_hold = estimate

        # Authorize-time pause enforcement belongs to the armed trust program,
        # not today's path. Shipping it unarmed changed the enclave rollout
        # gate's behavior in production.
        if trust_settings is not None and trust_settings.spend_lease_trust_eligibility_enabled:
            # Pause state is replicated atomically across the credit shards. Read
            # only the selected shard, whose balance DML already joined this txn's
            # read set; a workspace-wide scan couples otherwise independent holds
            # and can exhaust the retry budget under contention. BYOK / lease-
            # escrowed requests use shard zero. A pause still conflicts on this
            # shard and rejection rolls back every staged credit hold.
            from trusted_router.trust_eligibility import billing_paused_tx
            if billing_paused_tx(transaction, pt, workspace_id, shard=selected_credit_shard):
                raise _Reject("billing_paused")

        lease_result: dict[str, Any] = {
            "bound": False,
            "no_lease_reason": None,
            "spend_lease_outcome": None,
        }
        # The hook may escrow or release credit, including recovery/pause work.
        # Its writes share this transaction and roll back if the key rejects;
        # regional binding happens only after commit. Keep credit before key.
        if spend_lease_hook is not None:
            lease_result = spend_lease_hook(transaction, selected_credit_shard)
        if spend_lease_receipt_hash is not None and not lease_result.get("bound"):
            no_lease_reason = lease_result.get("no_lease_reason")
            if no_lease_reason == "scope_arbitrated":
                reason = "scope_conflict"
            elif no_lease_reason == "unpaid_workspace":
                reason = "hold_refused"
            else:
                reason = "reuse_lost"
            raise _Reject(f"admission_rejected:{reason}")

        # Bounded lifetime-cap TOCTOU: a cap committed after the gateway's
        # entity read can miss only requests already in flight at that commit,
        # each admitted for its own estimate (aggregate: sum of those estimates).
        # The next fresh entity read enforces the cap; removal likewise takes
        # effect on the next request. Do not add a hot api_key/counter read here.
        if skip_key_limit:
            key_result = KEY_NO_HOLD
            selected_key_shard = key_candidates[0]
        else:
            key_result = KEY_MISSING
            selected_key_shard = UNSHARDED
            saw_key_row = False
            for candidate in key_candidates:
                candidate_result = reserve_key(
                    transaction,
                    pt,
                    key_hash,
                    estimate,
                    is_byok=is_byok,
                    shard=candidate,
                )
                if candidate_result == KEY_MISSING:
                    continue
                saw_key_row = True
                if candidate_result == KEY_INSUFFICIENT:
                    continue
                key_result = candidate_result
                selected_key_shard = candidate
                break
            if key_result == KEY_MISSING:
                raise _Reject(
                    AuthorizeOutcome.KEY_LIMIT_EXCEEDED if saw_key_row else AuthorizeOutcome.KEY_MISSING
                )
        key_hold = estimate if key_result == KEY_ACCEPTED else 0

        reservation_statement = reservation_insert_statement(
            pt,
            reservation_id=reservation_id,
            workspace_id=workspace_id,
            key_hash=key_hash,
            ws_shard=selected_credit_shard,
            credit_shard=selected_credit_shard,
            key_shard=selected_key_shard,
            credit_reserved_micro=credit_hold,
            key_reserved_micro=key_hold,
            hold_usage_type=reservation_usage_type,
            authorization_id=authorization_id,
            idempotency_scope=idempotency_scope,
            idempotency_fingerprint=idempotency_fingerprint,
            expires_at=expires_at,
            created_at=created_at,
        )
        if request_record_write_mode == "typed":
            selected_authorization = authorization
            if build_authorization_for_lease is not None:
                selected_authorization = build_authorization_for_lease(
                    authorization_id,
                    reservation_id,
                    bool(lease_result.get("bound")),
                )
                selected_authorization.created_at = created_at.isoformat().replace(
                    "+00:00", "Z"
                )
            assert selected_authorization is not None
            authorization_statement = gateway_authorization_insert_statement(
                pt,
                selected_authorization,
                created_at=created_at,
            )
        else:
            assert legacy_auth_body is not None
            authorization_statement = entity_insert_statement(
                pt,
                "gateway_authorization",
                authorization_id,
                legacy_auth_body,
            )
        # Key reserve's row count is a business decision above, never speculative.
        execute_batch_dml(
            transaction, [reservation_statement, authorization_statement], [(1,), (1,)]
        )
        return {
            "outcome": AuthorizeOutcome.ACCEPTED,
            "reservation_id": reservation_id,
            "authorization_id": authorization_id,
            "credit_shard": selected_credit_shard,
            "key_shard": selected_key_shard,
            **lease_result,
        }

    try:
        return run_in_transaction_with_retry(
            database,
            txn,
            transaction_tag="tr_authorize",
            also_retry=also_retry,
        )
    except _Reject as reject:
        return {"outcome": reject.outcome}
    except AlreadyExists:
        # Concurrent first-call lost the unique-idempotency-index race: the winner
        # committed; re-read and replay (codex Step-3 #4) — never a second debit.
        # The conflict was on idempotency_scope, so it is necessarily non-None.
        assert idempotency_scope is not None
        conflict_scope: str = idempotency_scope

        def replay_txn(transaction: Any) -> dict:
            existing = read_reservation_by_idempotency(transaction, pt, conflict_scope)
            if existing is None:  # pragma: no cover - winner must exist post-conflict
                raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
            return _replay(transaction, existing)

        try:
            return run_in_transaction_with_retry(
                database,
                replay_txn,
                transaction_tag="tr_authorize_replay",
            )
        except _Reject as reject:
            return {"outcome": reject.outcome}
