"""Frozen main 6e793645 authorize and reserve helpers; AST-pinned by the oracle test."""
from __future__ import annotations

from trusted_router.storage_gcp_authorize import (
    KEY_ACCEPTED,
    KEY_INSUFFICIENT,
    KEY_MISSING,
    KEY_NO_HOLD,
    MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION,
    TXN_BUDGET_SECONDS,
    UNSHARDED,
    AlreadyExists,
    Any,
    AuthorizeOutcome,
    AuthorizeVerdict,
    Callable,
    GatewayAuthorization,
    KeyWindowLimitExceeded,
    Sequence,
    _Reject,
    _RetrySequentialKeyReserve,
    entity_insert_statement,
    execute_batch_dml,
    gateway_authorization_insert_statement,
    read_reservation_by_idempotency,
    reservation_insert_statement,
    run_in_transaction_with_retry,
    spanner_rpc_budget,
    utcnow,
    uuid,
)
from trusted_router.storage_gcp_batch_dml import DmlStatement


def reserve_credit(
    transaction: Any, param_types: Any, workspace_id: str, amount: int, *, shard: int = UNSHARDED
) -> bool:
    """Atomically reserve `amount` against the workspace credit balance.

    True = accepted (row-count 1); False = insufficient credits (row-count 0).
    """
    # Static table name (literal, not interpolated) + bound params only.
    sql = (
        "UPDATE tr_credit_balance SET reserved = reserved + @est "
        "WHERE workspace_id=@ws AND shard=@shard "
        "AND (total_credits - total_usage - reserved) >= @est"
    )
    count = transaction.execute_update(
        sql,
        params={"est": int(amount), "ws": workspace_id, "shard": shard},
        param_types={
            "est": param_types.INT64,
            "ws": param_types.STRING,
            "shard": param_types.INT64,
        },
    )
    return count == 1

def reserve_credit_with_pause(
    transaction: Any, param_types: Any, workspace_id: str, amount: int, *, shard: int = UNSHARDED
) -> tuple[bool, bool]:
    """Return (reserved, paused) from the affected credit row in one RPC.

    An empty result is insufficient credit, not pause evidence. The caller must
    finish its bounded shard search before applying the funded shard's verdict.
    """
    from trusted_router.trust_eligibility import billing_paused_row

    rows = list(transaction.execute_sql(
        "UPDATE tr_credit_balance SET reserved = reserved + @est "
        "WHERE workspace_id=@ws AND shard=@shard "
        "AND (total_credits - total_usage - reserved) >= @est "
        "THEN RETURN billing_pause_causes, pause_epoch",
        params={"est": int(amount), "ws": workspace_id, "shard": shard},
        param_types={
            "est": param_types.INT64,
            "ws": param_types.STRING,
            "shard": param_types.INT64,
        },
    ))
    return (True, billing_paused_row(rows[0])) if rows else (False, False)

def reserve_key_statement(
    param_types: Any, key_hash: str, amount: int, *, is_byok: bool,
    shard: int = UNSHARDED,
) -> DmlStatement:
    """The capped-key hold statement, shared by sequential and speculative DML."""
    sql = (
        "UPDATE tr_key_limit SET reserved = reserved + @est "
        "WHERE key_hash=@kh AND shard=@shard AND limit_micro IS NOT NULL "
        "AND (@is_byok = FALSE OR include_byok = TRUE) "
        "AND (limit_micro - usage - IF(include_byok, byok_usage, 0) - reserved) >= @est"
    )
    return (
        sql,
        {"est": int(amount), "kh": key_hash, "shard": shard, "is_byok": bool(is_byok)},
        {
            "est": param_types.INT64,
            "kh": param_types.STRING,
            "shard": param_types.INT64,
            "is_byok": param_types.BOOL,
        },
    )

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
    sql, params, types = reserve_key_statement(
        param_types, key_hash, amount, is_byok=is_byok, shard=shard,
    )
    count = transaction.execute_update(sql, params=params, param_types=types)
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
    return KEY_INSUFFICIENT  # capped and over the cap

@spanner_rpc_budget(TXN_BUDGET_SECONDS)
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
    speculate_key_limit: bool = True,
    strict_budget: bool = False,
    enforce_strict_windows: bool = True,
    authorization_id: str | None = None,
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

    `speculate_key_limit` only selects the transaction shape. Callers with
    already-loaded metadata can set it False for uncapped or BYOK-excluded keys.
    Unlike `skip_key_limit`, it retains every authoritative sequential check,
    including when that metadata is stale. Omission preserves speculation.

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
        if existing["idempotency_fingerprint"] != idempotency_fingerprint:
            raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
        return {
            "outcome": AuthorizeOutcome.REPLAY,
            "reservation_id": existing["reservation_id"],
            "authorization_id": existing["authorization_id"],
            "credit_shard": int(existing.get("credit_shard", UNSHARDED)),
            "key_shard": int(existing.get("key_shard", UNSHARDED)),
        }

    if strict_budget and key_candidates != (UNSHARDED,):
        raise ValueError("strict budgets require exactly one key shard")
    speculative = not strict_budget and not skip_key_limit and speculate_key_limit

    def check_key_prefix(counts: Sequence[int]) -> None:
        # Zero is ambiguous (missing, exhausted, uncapped, BYOK-excluded).
        # Even a later INSERT error must not override the key business decision.
        # ABORTED is handled first by execute_batch_dml and retries this callback.
        if counts and counts[0] == 0:
            raise _RetrySequentialKeyReserve("speculative key hold missed")

    def txn(transaction: Any) -> dict:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(transaction, pt, idempotency_scope)
            if existing is not None:
                return _replay(transaction, existing)

        # Reserve credit before key so both paths use the same counter lock order.
        # Reservation/authorization INSERTs below consume the selected shards and holds.
        credit_hold = 0
        selected_credit_shard = UNSHARDED
        armed = trust_settings is not None and trust_settings.spend_lease_trust_eligibility_enabled
        paused = False
        if has_credit_candidate:
            for candidate in shard_candidates:
                if armed:
                    reserved, paused = reserve_credit_with_pause(
                        transaction, pt, workspace_id, estimate, shard=candidate,
                    )
                else:
                    reserved = reserve_credit(transaction, pt, workspace_id, estimate, shard=candidate)
                if reserved:
                    selected_credit_shard = candidate
                    break
            else:
                raise _Reject(AuthorizeOutcome.INSUFFICIENT_CREDITS)
            credit_hold = estimate

        if armed:
            # Returning DML observes pause at the write on the selected row.
            # Returning both columns preserves the old SELECT's dependencies
            # on this key, without coupling unrelated credit shards. Spanner
            # locks cells: reading pause earlier can change race scheduling,
            # but a conflicting pause still has to serialize or abort/retry.
            # BYOK has no credit UPDATE and retains its shard-zero read.
            if not has_credit_candidate:
                from trusted_router.trust_eligibility import billing_paused_tx

                paused = billing_paused_tx(transaction, pt, workspace_id, shard=selected_credit_shard)
            if paused:
                raise _Reject("billing_paused")

        # Bounded lifetime-cap TOCTOU: a cap committed after the gateway's
        # entity read can miss only requests already in flight at that commit,
        # each admitted for its own estimate (aggregate: sum of those estimates).
        # The next fresh entity read enforces the cap; removal likewise takes
        # effect on the next request. Do not add a hot api_key/counter read here.
        strict_decision = None
        if strict_budget:
            from trusted_router.storage_gcp_strict_budget import reserve_strict_key

            key_result, strict_decision = reserve_strict_key(
                transaction, pt, key_hash, estimate, is_byok=is_byok,
                enforce_windows=enforce_strict_windows,
            )
            selected_key_shard = UNSHARDED
            if key_result in {KEY_MISSING, KEY_INSUFFICIENT}:
                raise _Reject(AuthorizeVerdict(
                    AuthorizeOutcome.KEY_MISSING if key_result == KEY_MISSING else AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
                    rate_limit=strict_decision,
                ))
        elif speculative:
            key_result = KEY_ACCEPTED
            selected_key_shard = key_candidates[0]
        elif skip_key_limit:
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
            assert authorization is not None
            authorization_statement = gateway_authorization_insert_statement(
                pt,
                authorization,
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
        if speculative:
            # Ordered server execution: credit precedes key,
            # and key precedes these new rows. A zero does NOT stop Batch DML.
            execute_batch_dml(
                transaction,
                [reserve_key_statement(
                    pt, key_hash, estimate, is_byok=is_byok, shard=selected_key_shard,
                ), reservation_statement, authorization_statement],
                [(1,), (1,), (1,)],
                check_prefix=check_key_prefix,
            )
        else:
            execute_batch_dml(
                transaction, [reservation_statement, authorization_statement], [(1,), (1,)]
            )
        return {
            "outcome": AuthorizeVerdict(AuthorizeOutcome.ACCEPTED, rate_limit=strict_decision),
            "reservation_id": reservation_id,
            "authorization_id": authorization_id,
            "credit_shard": selected_credit_shard,
            "key_shard": selected_key_shard,
        }

    try:
        try:
            return run_in_transaction_with_retry(
                database, txn, transaction_tag="tr_authorize",
            )
        except _RetrySequentialKeyReserve:
            # Protected API-error cleanup attempted rollback before the SDK
            # discarded the handle. Cleanup never renews the shared T1 budget.
            # Retry the original decision path once; it handles no-hold success,
            # all shard candidates, and terminal rejection without speculation.
            # IDs, created_at, and candidate order remain stable across attempts.
            speculative = False
            return run_in_transaction_with_retry(
                database, txn, transaction_tag="tr_authorize",
            )
    except KeyWindowLimitExceeded as exceeded:
        return {"outcome": AuthorizeVerdict(
            f"{AuthorizeOutcome.KEY_WINDOW_LIMIT_EXCEEDED}:{exceeded.window}", rate_limit=exceeded.decision,
        )}
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
