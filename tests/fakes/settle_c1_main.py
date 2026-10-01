"""Frozen C1 money-path oracle from origin/main 8ee7985ecfcee781d39c7964173ce65ebd62bfc2.

Finalize, credit release, key release and key classification copied verbatim;
imports redirect releases to these frozen functions. Do not update with C1.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from trusted_router.spend_windows import (
    utcnow,
    window_floors,
)
from trusted_router.storage_gcp_authorize import (
    SettleOutcome,
    _apply_app_markup_payout_tx,
    _apply_custom_model_markup_payout_tx,
    _apply_user_model_payout_tx,
    _log_missing_key_releases,
    _outbox_table_available,
    _RetrySequentialFinalize,
    _SettleError,
)
from trusted_router.storage_gcp_batch_dml import DmlStatement, execute_batch_dml
from trusted_router.storage_gcp_counter_dml import (
    _credit_shard_count_from_rows,
    claim_reservation_statement,
    complete_reservation_retention,
)
from trusted_router.storage_gcp_counters import UNSHARDED
from trusted_router.storage_gcp_generation_records import (
    generation_insert_statement,
)
from trusted_router.storage_gcp_io import (
    TXN_BUDGET_SECONDS,
    remaining_rpc_budget,
    run_in_transaction_with_retry,
    spanner_rpc_budget,
)
from trusted_router.storage_gcp_request_records import (
    gateway_authorization_settled_statement,
    mark_gateway_authorization_settled,
)
from trusted_router.storage_gcp_settle_outbox import (
    mark_done_unleased_tx,
    speculative_done_statements,
)
from trusted_router.storage_models import (
    AppMarkupPayout,
    CustomModelMarkupPayout,
    GatewayAuthorization,
    Generation,
    UserModelPayout,
)

# Literal SQL from the frozen main revision: never import production money
# expressions here, or the differential shares the bug it is meant to detect.
_WINDOW_BUMP_SQL = (
    ", day_usage = IF(day_start IS NULL OR day_start < @day_floor,"
    " @day_wamt, COALESCE(day_usage, 0) + @day_wamt)"
    ", day_start = IF(day_start IS NULL OR day_start < @day_floor, @day_floor, day_start)"
    ", week_usage = IF(week_start IS NULL OR week_start < @week_floor,"
    " @week_wamt, COALESCE(week_usage, 0) + @week_wamt)"
    ", week_start = IF(week_start IS NULL OR week_start < @week_floor, @week_floor, week_start)"
    ", month_usage = IF(month_start IS NULL OR month_start < @month_floor,"
    " @month_wamt, COALESCE(month_usage, 0) + @month_wamt)"
    ", month_start = IF(month_start IS NULL OR month_start < @month_floor, @month_floor, month_start)"
)

# The common settle path has already-current window boundaries. Keep those
# boundary columns out of its SET list so Spanner does not take exclusive cell
# locks on values that did not change. The guarded UPDATE below falls back to
# _WINDOW_BUMP_SQL whenever any boundary needs its lazy roll.
_CURRENT_WINDOW_BUMP_SQL = (
    ", day_usage = COALESCE(day_usage, 0) + @day_wamt"
    ", week_usage = COALESCE(week_usage, 0) + @week_wamt"
    ", month_usage = COALESCE(month_usage, 0) + @month_wamt"
)
_CURRENT_WINDOW_PREDICATE_SQL = (
    " AND day_start IS NOT NULL AND day_start >= @day_floor"
    " AND week_start IS NOT NULL AND week_start >= @week_floor"
    " AND month_start IS NOT NULL AND month_start >= @month_floor"
)


log = logging.getLogger(__name__)


def release_credit(
    transaction: Any,
    param_types: Any,
    workspace_id: str,
    hold: int,
    actual: int,
    *,
    shard: int = UNSHARDED,
) -> int:
    """Release the EXACT recorded credit hold and book `actual` usage.

    For refund pass actual=0 (releases the hold, books no usage). Returns the
    modified-row count; the caller asserts == 1 (a 0-row release must not be
    silently accepted — it strands the hold and loses the charge).

    The `reserved >= @hold` guard makes a stale/double release a 0-row no-op
    instead of driving `reserved` negative (which would inflate apparent
    availability) — row-count 0 trips the caller's assert/alarm.
    """
    sql = (
        "UPDATE tr_credit_balance "
        "SET reserved = reserved - @hold, total_usage = total_usage + @actual "
        "WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold"
    )
    count = transaction.execute_update(
        sql,
        params={"hold": int(hold), "actual": int(actual), "ws": workspace_id, "shard": shard},
        param_types={
            "hold": param_types.INT64,
            "actual": param_types.INT64,
            "ws": param_types.STRING,
            "shard": param_types.INT64,
        },
    )
    free = int(hold) - int(actual)
    if count == 1 and free > 0:
        from trusted_router.storage_gcp_trust import absorb_unrecovered_recovery_tx

        absorbed = absorb_unrecovered_recovery_tx(
            transaction,
            param_types,
            workspace_id=workspace_id,
            amount_micro=free,
            # Lazy on purpose (#1071 follow-up): the shard-set read takes
            # ReaderShared on EVERY credit shard while this transaction holds
            # Exclusive on one of them — the cross-shard X-on-mine/S-on-yours
            # wait that defeats credit sharding — and costs one RPC per
            # settle. absorb only needs it when a payment claim exists.
            shard_count=lambda: _credit_shard_count_from_rows(
                transaction, param_types, workspace_id
            ),
            now=datetime.now(UTC),
            read_entity_tx=None,
            write_entity_tx=None,
        )
        if absorbed:
            debited = transaction.execute_update(
                "UPDATE tr_credit_balance SET total_credits=total_credits-@amount "
                "WHERE workspace_id=@ws AND shard=@shard "
                "AND (total_credits-total_usage-reserved)>=@amount",
                params={
                    "amount": absorbed,
                    "ws": workspace_id,
                    "shard": shard,
                },
                param_types={
                    "amount": param_types.INT64,
                    "ws": param_types.STRING,
                    "shard": param_types.INT64,
                },
            )
            if int(debited) != 1:
                raise RuntimeError("released credit could not satisfy recovery debt")
    return count

def release_key(
    transaction: Any,
    param_types: Any,
    key_hash: str,
    hold: int,
    actual: int,
    *,
    book_to_byok: bool,
    window_floors: dict[str, Any],
    shard: int = UNSHARDED,
) -> int:
    """Release the EXACT recorded key hold and book `actual` to usage/byok_usage,
    and bump the lazy per-window counters in the same statement.

    `hold` is the exact amount taken at reserve (0 if no hold was taken — uncapped
    or BYOK-excluded); `book_to_byok` selects the usage column by the SETTLED
    usage type. Refund = actual 0 (window bump is then +0 — a no-op that still
    lazily rolls the window forward, which is harmless). `window_floors` is
    spend_windows.window_floors(now). The `reserved >= @hold` guard makes a
    stale/double release a 0-row no-op rather than driving reserved negative.
    Returns the modified-row count (caller asserts == 1).
    """
    usage_col = "byok_usage" if book_to_byok else "usage"
    # BYOK settles count toward the caps (incl. windows) only when the key's own
    # include_byok says so — gated in SQL so it matches reserve semantics. On an
    # excluded settle the bump is +0, but a stale window still rolls forward.
    wamt = "IF(include_byok, @actual, 0)" if book_to_byok else "@actual"
    # usage_col/wamt are compile-time constants picked by a bool; values bind as params.
    params = {
        "hold": int(hold),
        "actual": int(actual),
        "kh": key_hash,
        "shard": shard,
        "day_floor": window_floors["daily"],
        "week_floor": window_floors["weekly"],
        "month_floor": window_floors["monthly"],
    }
    bound_param_types = {
        "hold": param_types.INT64,
        "actual": param_types.INT64,
        "kh": param_types.STRING,
        "shard": param_types.INT64,
        "day_floor": param_types.TIMESTAMP,
        "week_floor": param_types.TIMESTAMP,
        "month_floor": param_types.TIMESTAMP,
    }
    current_window_sql = _CURRENT_WINDOW_BUMP_SQL
    rolled_window_sql = _WINDOW_BUMP_SQL
    for window in ("day", "week", "month"):
        current_window_sql = current_window_sql.replace(f"@{window}_wamt", wamt)
        rolled_window_sql = rolled_window_sql.replace(f"@{window}_wamt", wamt)
    fast_sql = (
        "UPDATE tr_key_limit "  # noqa: S608
        f"SET reserved = reserved - @hold, {usage_col} = {usage_col} + @actual"
        + current_window_sql
        + " WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold"
        + _CURRENT_WINDOW_PREDICATE_SQL
    )
    fast_count = transaction.execute_update(
        fast_sql,
        params=params,
        param_types=bound_param_types,
    )
    if fast_count == 1:
        return 1

    # Keep this fallback statement identical to the original release UPDATE.
    sql = (
        "UPDATE tr_key_limit "  # noqa: S608
        f"SET reserved = reserved - @hold, {usage_col} = {usage_col} + @actual"
        + rolled_window_sql
        + " WHERE key_hash=@kh AND shard=@shard AND reserved >= @hold"
    )
    return transaction.execute_update(
        sql,
        params=params,
        param_types=bound_param_types,
    )

def _release_key_or_skip_deleted(
    transaction: Any,
    param_types: Any,
    res: dict[str, Any],
    actual_micro: int,
    *,
    book_to_byok: bool,
) -> tuple[int, dict[str, Any] | None]:
    """Shared key-release classification for settle, reaper, and drain paths.

    `release_key` deliberately returns the raw UPDATE count. A 0 count is
    ambiguous only here, after the reservation has been claimed: the key row may
    have been deleted, or the `reserved >= hold` corruption guard may have fired.
    Zero-hold usage (including skip-path reservations) must book even after a
    shrink reshard: recover on shard zero or raise with the amount preserved.
    Held/deleted keys and zero-usage refunds retain their historical behavior.
    """
    from trusted_router.storage_gcp_counter_dml import key_limit_exists

    key_hash = str(res["key_hash"])
    key_hold = int(res["key_reserved_micro"])
    key_shard = int(res.get("key_shard", 0) or 0)
    floors = window_floors(utcnow())
    count = release_key(
        transaction,
        param_types,
        key_hash,
        key_hold,
        int(actual_micro),
        book_to_byok=book_to_byok,
        window_floors=floors,
        shard=key_shard,
    )
    if count == 1:
        return count, None
    # Pre-migration credit-only reservations can legitimately have no key.
    if res["key_hash"] is not None and key_hold == 0 and actual_micro > 0:
        if key_shard != 0:
            recovered = release_key(
                transaction, param_types, key_hash, 0, int(actual_micro),
                book_to_byok=book_to_byok, window_floors=floors,
                shard=0,
            )
            if recovered == 1:
                return 1, {
                    "key_hash": key_hash, "hold_micro": 0,
                    "missing_shard": key_shard, "actual_micro": int(actual_micro),
                }
        # Deliberately not _SettleError: callers must see the amount and retry
        # (the outbox retains its frozen payload), never report SETTLED.
        raise RuntimeError(
            f"key usage booking failed reservation_id={res['reservation_id']} "
            f"key_hash={key_hash} shard={key_shard} fallback_shard=0 "
            f"actual_micro={actual_micro} book_to_byok={book_to_byok}"
        )
    if key_limit_exists(transaction, param_types, key_hash, shard=key_shard):
        return count, None
    return 1, {"key_hash": key_hash, "hold_micro": key_hold}

@spanner_rpc_budget(TXN_BUDGET_SECONDS)
def typed_finalize_atomic(
    database: Any,
    param_types: Any,
    *,
    reservation_id: str,
    authorization_id: str,
    success: bool,
    actual_micro: int,
    settled_usage_type: str,
    now: Any,
    outbox_available: bool | None = None,
    authorization: GatewayAuthorization | None = None,
    auth_body_settled: str,
    generation_writes: list[tuple[str, str, str]] | None = None,
    generation: Generation | None = None,
    persist_generation_record: bool = False,
    operational_analytics_outbox: Any | None = None,
    user_model_payout: UserModelPayout | None = None,
    app_markup_payout: AppMarkupPayout | None = None,
    custom_model_markup_payout: CustomModelMarkupPayout | None = None,
    settle_outbox_done: tuple[str, str] | None = None,
) -> dict:
    """Full DML-only finalize for the typed path (codex 3e, Option B).

    ``settle_outbox_done=(authorization_id, intent_kind)`` also resolves that
    durable settle-outbox intent to ``done`` in the SAME transaction, when the
    activity row is durable in this commit and the outbox table exists. The
    result carries ``outbox_marked``: True (marked), False (row leased or
    already resolved -- the drain re-derives), or None (not attempted).

    ONE transaction reproduces legacy finalize_gateway_authorization's whole
    behavior so a crash can't leave counters charged but the authorization
    active: claim the reservation -> DML-mark the authorization settled ->
    release the EXACT holds and book actual (credit then key). Typed request records
    keep their repair payload until durable analytics delivery is confirmed;
    rolling legacy records retain the old generic generation repair rows.

    `auth_body_settled` and `generation_writes` remain for rolling compatibility
    with an authorization created by the generic-table revision. Returns
    {outcome: settled|already_settled|not_found|error}.
    """
    from trusted_router.storage_gcp_counter_dml import (
        claim_reservation,
        insert_entity_dml_at,
        read_reservation,
        update_entity_body_dml,
    )
    pt = param_types
    book_actual = actual_micro if success else 0
    book_to_byok = settled_usage_type == "BYOK"
    writes = generation_writes or []
    resolved_outbox_available = (
        _outbox_table_available(database, pt) if outbox_available is None else outbox_available
    )

    # Known legacy inputs and custom outbox callbacks keep the sequential path.
    speculate = (
        authorization is not None
        and (operational_analytics_outbox is None or callable(
            getattr(operational_analytics_outbox, "activity_insert_statement", None)
        ))
    )
    activity_durable = generation is None or operational_analytics_outbox is not None
    mark_done = (
        settle_outbox_done is not None and resolved_outbox_available and activity_durable
    )
    attempts = 0
    eligible_attempts = 0
    fallback_reason = "none"
    rollback_ms = 0.0
    fallback_outcome = "not_attempted"

    def speculative_batch(transaction: Any, *, include_claim: bool) -> None:
        nonlocal eligible_attempts
        eligible_attempts += 1
        assert authorization is not None
        statements = []
        reasons = []
        if include_claim:
            statements.append(claim_reservation_statement(
                pt, reservation_id, actual_micro=book_actual,
                settled_usage_type=settled_usage_type, terminal_at=now,
                defer_retention=True, outbox_available=resolved_outbox_available,
            ))
            reasons.append("claim_zero")
        statements.append(gateway_authorization_settled_statement(pt, authorization))
        reasons.append("typed_zero")
        counts: list[tuple[int, ...]] = [(1,)] * len(statements)
        if mark_done:
            assert settle_outbox_done is not None
            done = speculative_done_statements(
                pt, authorization_id=settle_outbox_done[0], intent_kind=settle_outbox_done[1],
                reservation_id=reservation_id,
            )
            statements.extend(done)
            counts.extend([(1,), *[(0, 1)] * (len(done) - 1)])
            reasons.append("done_zero")
        if success and generation is not None:
            if persist_generation_record:
                statements.append(generation_insert_statement(pt, generation, terminal_at=now))
                counts.append((1,))
            if operational_analytics_outbox is not None:
                # PENDING_COMMIT_TIMESTAMP is the last analytics access.
                statements.append(operational_analytics_outbox.activity_insert_statement(generation))
                counts.append((1,))

        def check_prefix(row_counts: Sequence[int]) -> None:
            for count, reason in zip(row_counts, reasons, strict=False):
                if count == 0:
                    raise _RetrySequentialFinalize(reason)
                if count != 1:
                    # A malformed earlier count cannot authorize a later fallback.
                    break

        execute_batch_dml(transaction, statements, counts, check_prefix=check_prefix)

    def txn(transaction: Any) -> dict:
        nonlocal attempts
        attempts += 1
        res = read_reservation(transaction, pt, reservation_id)
        if res is None:
            return {"outcome": SettleOutcome.NOT_FOUND}
        if speculate:
            speculative_batch(transaction, include_claim=True)
        else:
            won = claim_reservation(
                transaction,
                pt,
                reservation_id,
                actual_micro=book_actual,
                settled_usage_type=settled_usage_type,
                terminal_at=now,
                defer_retention=True,
                outbox_available=resolved_outbox_available,
            )
            if not won:
                return {"outcome": SettleOutcome.ALREADY_SETTLED}

        if success and user_model_payout is not None and user_model_payout.amount_microdollars > 0:
            # Deliberately NOT wrapped in a swallow. The payout is two DML
            # statements in this same transaction; a failure between them
            # would otherwise commit the movement row without the balance
            # bump (or the reverse), and every later replay would read the
            # row as "already paid" — a permanent, silent underpayment. An
            # exception here aborts the whole finalize: transient errors are
            # retried by run_in_transaction_with_retry, and a deterministic
            # one (schema drift) fails the settle LOUDLY, which the outbox
            # repair/drain surfaces, instead of quietly not paying owners.
            _apply_user_model_payout_tx(
                transaction,
                pt,
                authorization_id=authorization_id,
                payout=user_model_payout,
                now=now,
            )
        if success and app_markup_payout is not None and app_markup_payout.amount_microdollars > 0:
            _apply_app_markup_payout_tx(
                transaction,
                pt,
                authorization_id=authorization_id,
                payout=app_markup_payout,
                now=now,
            )
        if (
            success
            and custom_model_markup_payout is not None
            and custom_model_markup_payout.amount_microdollars > 0
        ):
            _apply_custom_model_markup_payout_tx(
                transaction,
                pt,
                authorization_id=authorization_id,
                payout=custom_model_markup_payout,
                now=now,
            )

        if speculate:
            request_record_typed = True
            outbox_marked: bool | None = True if mark_done else None
        else:
            marked = 0
            request_record_typed = False
            if authorization is not None:
                marked = mark_gateway_authorization_settled(
                    transaction,
                    pt,
                    authorization,
                )
                request_record_typed = marked == 1
            if not request_record_typed:
                if success:
                    for kind, entity_id, body_json in writes:
                        insert_entity_dml_at(
                            transaction,
                            pt,
                            kind,
                            entity_id,
                            body_json,
                            now,
                        )
                marked = update_entity_body_dml(
                    transaction,
                    pt,
                    "gateway_authorization",
                    authorization_id,
                    auth_body_settled,
                    now,
                )
                complete_reservation_retention(
                    transaction,
                    pt,
                    reservation_id,
                    terminal_at=now,
                    outbox_available=resolved_outbox_available,
                )
            outbox_marked = None
            final_writes: list[DmlStatement] = []
            final_counts: list[tuple[int, ...]] = []
            if settle_outbox_done is not None and resolved_outbox_available and activity_durable:
                # Same commit as the charge: no post-finalize window in which a
                # crash leaves an already-charged authorization pending, and one
                # fewer multi-region round trip per settle. A leased row is left
                # to its drain worker (False), exactly as the standalone mark did.
                outbox_marked = mark_done_unleased_tx(
                    transaction, pt, authorization_id=settle_outbox_done[0],
                    intent_kind=settle_outbox_done[1], retention_statements=final_writes,
                )
                final_counts.extend([(0, 1)] * len(final_writes))
            if success and generation is not None:
                if persist_generation_record:
                    final_writes.append(generation_insert_statement(pt, generation, terminal_at=now))
                    final_counts.append((1,))
                if operational_analytics_outbox is not None:
                    # Preserve rolling/custom outbox implementations without the
                    # statement-building API. Flush in the original write order.
                    builder = getattr(operational_analytics_outbox, "activity_insert_statement", None)
                    if builder is not None:
                        final_writes.append(builder(generation))
                        final_counts.append((1,))
                    else:
                        if final_writes:
                            execute_batch_dml(transaction, final_writes, final_counts)
                            final_writes.clear()
                            final_counts.clear()
                        operational_analytics_outbox.enqueue_activity_tx(transaction, generation)
            if final_writes:
                execute_batch_dml(transaction, final_writes, final_counts)
            if marked != 1:
                raise _SettleError("gateway_authorization update row-count != 1")

        # Hot-row releases LAST, in the SAME transaction (never split: separating
        # claim_reservation(settled=true) from these releases opens a crash
        # window that strands a settled reservation's hold or double-releases
        # on retry). tr_key_limit / tr_credit_balance are the rows every
        # concurrent settle of one key/workspace serializes on; taking their
        # Exclusive locks after the authorization, outbox, generation and
        # activity writes holds them for ~2 RPCs + commit instead of ~12 RPCs +
        # commit. Credit goes first so its contention cannot extend the key lock.
        # The `!= 1` raises still abort the whole transaction.
        missing_key_releases = []
        if res["credit_reserved_micro"] > 0:
            credit_actual = book_actual if settled_usage_type == "Credits" else 0
            credit_count = release_credit(
                transaction,
                pt,
                res["workspace_id"],
                res["credit_reserved_micro"],
                credit_actual,
                shard=res["credit_shard"],
            )
            if credit_count != 1:
                raise _SettleError("credit release row-count != 1")

        key_count, warning = _release_key_or_skip_deleted(
            transaction, pt, res, book_actual, book_to_byok=book_to_byok,
        )
        if warning is not None:
            missing_key_releases.append(warning)
        if res["key_reserved_micro"] > 0 and key_count != 1:
            raise _SettleError("key release row-count != 1")

        return {
            "outcome": SettleOutcome.SETTLED,
            "missing_key_releases": missing_key_releases,
            "request_record_typed": request_record_typed,
            "activity_durable": activity_durable,
            "outbox_marked": outbox_marked,
        }

    def run() -> dict:
        return run_in_transaction_with_retry(
            database, txn,
            transaction_tag="tr_finalize" if success else "tr_refund_finalize",
        )

    try:
        try:
            result = run()
        except _RetrySequentialFinalize as exc:
            # The runner has completed protected rollback and discarded T3.
            # Never carry S11 across this boundary: deletion can legitimately
            # change ALREADY_SETTLED into NOT_FOUND on the fresh observation.
            rollback_ms = (time.monotonic() - exc.rollback_started) * 1000
            fallback_reason = exc.fallback_reason
            speculate = False
            fallback_outcome = "exception"
            result = run()
            fallback_outcome = result["outcome"]
        result["attempts"] = attempts
        _log_missing_key_releases(result)
        return result
    except _SettleError:
        if fallback_reason != "none":
            fallback_outcome = SettleOutcome.ERROR
        return {"outcome": SettleOutcome.ERROR}
    finally:
        if eligible_attempts:
            from google.api_core.exceptions import DeadlineExceeded

            try:
                remaining_ms = remaining_rpc_budget(TXN_BUDGET_SECONDS) * 1000
            except DeadlineExceeded:
                remaining_ms = 0.0
            log.info(
                "typed finalize speculation timing eligible_attempts=%d attempts=%d "
                "fallback_reason=%s rollback_ms=%.1f remaining_ms=%.1f fallback_outcome=%s",
                eligible_attempts, attempts, fallback_reason, rollback_ms,
                remaining_ms, fallback_outcome,
            )

