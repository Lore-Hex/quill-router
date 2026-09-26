"""Frozen T3 before item 6b; independent control flow for differential tests."""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from trusted_router.storage_gcp_authorize import (
    SettleOutcome,
    _apply_app_markup_payout_tx,
    _apply_custom_model_markup_payout_tx,
    _apply_user_model_payout_tx,
    _log_missing_key_releases,
    _outbox_table_available,
    _release_key_or_skip_deleted,
    _SettleError,
)
from trusted_router.storage_gcp_batch_dml import DmlStatement, execute_batch_dml
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counter_dml import (
    _CLAIM_RESERVATION_GUARDED_SQL,
    _CLAIM_RESERVATION_SQL,
    complete_reservation_retention,
)
from trusted_router.storage_gcp_generation_records import generation_insert_statement
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_request_records import (
    _AUTHORIZATION_HEARTBEAT_FIELDS,
    _SETTLED_PAYLOAD_SQL,
    authorization_typed_columns,
)
from trusted_router.storage_gcp_settle_outbox import (
    mark_done_unleased_tx,
    rewrite_frozen_settlement_tx,
)
from trusted_router.storage_models import (
    AppMarkupPayout,
    CustomModelMarkupPayout,
    GatewayAuthorization,
    Generation,
    UserModelPayout,
)


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
    regional_hold_unknown: bool = False,
    regional_global_micro: int = 0,
    finalize_regional_hold: Callable[[], tuple[bool, int, datetime | None]] | None = None,
    settle_outbox_done: tuple[str, str] | None = None,
    settle_outbox_rewrite: tuple[str, str, str, int, str] | None = None,
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
        insert_entity_dml_at,
        read_reservation,
        release_credit,
        update_entity_body_dml,
    )
    from trusted_router.storage_gcp_regional_quota import _RegionalWindowAdvanced

    pt = param_types
    book_actual = actual_micro if success else 0
    book_to_byok = settled_usage_type == "BYOK"
    writes = generation_writes or []
    resolved_outbox_available = (
        _outbox_table_available(database, pt) if outbox_available is None else outbox_available
    )

    def txn(transaction: Any) -> dict:
        res = read_reservation(transaction, pt, reservation_id)
        if res is None:
            return {"outcome": SettleOutcome.NOT_FOUND}
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
            return {
                "outcome": SettleOutcome.ALREADY_SETTLED,
                "regional_terminal_zero": (
                    finalize_regional_hold is not None and res.get("actual_micro") == 0
                ),
            }

        # Resolve the terminal winner BEFORE any external local CAS. The
        # reservation claim serializes us with the reaper; a durable frozen
        # intent protects a local commit if this transaction later aborts.
        hold_unknown, global_micro, settled_at = regional_hold_unknown, regional_global_micro, None
        if finalize_regional_hold is not None:
            hold_unknown, global_micro, settled_at = finalize_regional_hold()

        if settle_outbox_rewrite is not None:
            rewrite_aid, rewrite_kind, lease_owner, rewrite_cost, rewrite_body = (
                settle_outbox_rewrite
            )
            rewritten = rewrite_frozen_settlement_tx(
                transaction,
                pt,
                authorization_id=rewrite_aid,
                intent_kind=rewrite_kind,
                lease_owner=lease_owner,
                actual_cost_micro=rewrite_cost,
                settle_body=rewrite_body,
                now=now,
            )
            if rewritten != 1:
                raise _SettleError("corrective settle-outbox rewrite lost its lease fence")

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
        activity_durable = generation is None or operational_analytics_outbox is not None
        outbox_marked: bool | None = None
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
        elif (hold_unknown or global_micro > 0) and res.get("hold_usage_type") == "RegionalCredits":
            # Healthy overruns book ONLY the unbacked excess here. The local
            # component remains in escrow until reconciliation. If a stale CAS
            # erased the hold, book the entire charge under this same claim;
            # closing reconciliation releases the missing hold's unused escrow.
            credit_actual = (book_actual if hold_unknown else global_micro) if settled_usage_type == "Credits" else 0
            credit_count = release_credit(
                transaction,
                pt,
                res["workspace_id"],
                0,
                credit_actual,
                shard=res["credit_shard"],
            )
            if credit_count != 1:
                raise _SettleError("regional fallback credit booking row-count != 1")

        # Authorization is already loaded for finalization. No lease read belongs
        # in this transaction. Missing versions retain the V1 inline contract;
        # a missing Bigtable hold always uses the existing claimed recovery path.
        regional_reconciler_owns_key = (
            res.get("hold_usage_type") == "RegionalCredits"
            and authorization is not None
            and authorization.regional_accounting_version == 2
            and not hold_unknown
        )
        # V2 imports the local component with the lease; only its excess is
        # inline. V1 and missing-hold recovery still own the entire key charge.
        key_actual = global_micro if regional_reconciler_owns_key else book_actual
        if not regional_reconciler_owns_key or key_actual > 0:
            key_count, warning = _release_key_or_skip_deleted(
                transaction, pt, res, key_actual, book_to_byok=book_to_byok,
                settled_at=settled_at,
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

    try:
        attempts_box: list[int] = []
        result = run_in_transaction_with_retry(
            database,
            txn,
            attempts_out=attempts_box,
            transaction_tag="tr_finalize" if success else "tr_refund_finalize",
            also_retry=(_RegionalWindowAdvanced,),
        )
        result["attempts"] = attempts_box[0] if attempts_box else 1
        _log_missing_key_releases(result)
        return result
    except _SettleError:
        return {"outcome": SettleOutcome.ERROR}



def claim_reservation(
    transaction: Any,
    param_types: Any,
    reservation_id: str,
    *,
    actual_micro: int,
    settled_usage_type: str,
    terminal_at: Any | None = None,
    defer_retention: bool = False,
    outbox_available: bool = True,
    expires_before: Any | None = None,
) -> bool:
    """Claim a reservation for settle/refund: first caller wins.

    True = this caller won the claim (row-count 1, settled flipped false->true);
    False = already settled (row-count 0, a replay) -> do NOT touch counters.
    Persists `actual_micro` + `settled_usage_type` so the durable reservation
    records the exact settled amount for audit / reaper reconciliation.
    """
    resolved_terminal_at = (
        None if defer_retention else (terminal_at or datetime.now(UTC))
    )
    sql = _CLAIM_RESERVATION_GUARDED_SQL if outbox_available else _CLAIM_RESERVATION_SQL
    params = {
            "rid": reservation_id,
            "actual": int(actual_micro),
            "sut": settled_usage_type,
            "terminal_at": resolved_terminal_at,
        }
    types = {
            "rid": param_types.STRING,
            "actual": param_types.INT64,
            "sut": param_types.STRING,
            "terminal_at": param_types.TIMESTAMP,
        }
    if expires_before is not None:
        # The reaper's snapshot scan is advisory. This predicate is the final
        # row-count guard, inside the same read-write transaction as booking;
        # a heartbeat renewal after the scan therefore cannot lose its hold.
        sql += " AND expires_at < @reap_now"
        params["reap_now"] = expires_before
        types["reap_now"] = param_types.TIMESTAMP
    count = transaction.execute_update(sql, params=params, param_types=types)
    return count == 1


def mark_gateway_authorization_settled(
    transaction: Any,
    param_types: Any,
    authorization: GatewayAuthorization,
) -> int:
    """Mark billing settled while keeping repair metadata and TTL disabled."""
    typed = authorization_typed_columns(dataclasses.asdict(authorization))
    payload = json.loads(json_body(authorization))
    for column in _AUTHORIZATION_HEARTBEAT_FIELDS:
        payload.pop(column, None)
    serialized_payload = json_body(payload)
    payload_error = "settled @payload must be a nonempty JSON object without heartbeat keys"
    try:
        payload_object = json.loads(serialized_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(payload_error) from exc
    if (
        not serialized_payload.startswith("{")
        or not serialized_payload.endswith("}")
        or not isinstance(payload_object, dict)
        or not payload_object
        or any(column in payload_object for column in _AUTHORIZATION_HEARTBEAT_FIELDS)
    ):
        raise ValueError(payload_error)
    return transaction.execute_update(
        f"UPDATE tr_gateway_authorization SET settled=true, payload={_SETTLED_PAYLOAD_SQL}, "  # noqa: S608
        "finalization_outcome=@finalization_outcome, "
        "finalized_cost_microdollars=@finalized_cost_microdollars, "
        "gateway_request_id=@gateway_request_id "
        "WHERE authorization_id=@authorization_id AND settled=false",
        params={
            "authorization_id": authorization.id,
            "payload": serialized_payload,
            "finalization_outcome": typed["finalization_outcome"],
            "finalized_cost_microdollars": typed["finalized_cost_microdollars"],
            "gateway_request_id": typed["gateway_request_id"],
        },
        param_types={
            "authorization_id": param_types.STRING,
            "payload": param_types.STRING,
            "finalization_outcome": param_types.STRING,
            "finalized_cost_microdollars": param_types.INT64,
            "gateway_request_id": param_types.STRING,
        },
    )
