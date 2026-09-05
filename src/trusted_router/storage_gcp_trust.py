"""Typed Spanner trust facts and the all-shard trust-tier computation."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

from trusted_router.storage_gcp_counter_dml import credit_credit_shard
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TRUST_COLUMNS,
    credit_shard_count,
    distribute_credit_amount,
)
from trusted_router.storage_models import (
    AdverseTrustEvent,
    AdverseTrustResult,
    CreditAccount,
    TrustEvent,
    User,
    Workspace,
)
from trusted_router.trust_tiers import (
    TRUST_PAUSE_CAUSES,
    adverse_event_from_payload,
    adverse_event_payload,
    adverse_transition_outcome,
    compute_trust_tier,
    payment_recovery_target,
    trust_inbox_reference,
    validate_adverse_event,
)

TRUST_EVENT_COLUMNS = (
    "workspace_id",
    "event_id",
    "kind",
    "provider",
    "amount_micro",
    "original_payment_ref",
    "adverse_ref",
    "occurred_at",
    "recorded_at",
    "payment_amount_micro",
    "currency",
    "credited_micro",
    "recovered_micro",
    "provider_subtype",
    "lifecycle_status",
    "cumulative_refunded",
    "recovery_target",
    "debit_status",
    "unrecovered_micro",
    "provider_ordering_watermark",
)


def trust_event_row(event: TrustEvent) -> tuple[Any, ...]:
    return tuple(getattr(event, column) for column in TRUST_EVENT_COLUMNS)


def insert_credit_trust_event(
    transaction: Any,
    param_types: Any,
    event: TrustEvent,
) -> bool:
    inserted = transaction.execute_update(
        "INSERT INTO tr_trust_event (workspace_id, event_id, kind, provider, "
        "amount_micro, original_payment_ref, adverse_ref, occurred_at, recorded_at, "
        "payment_amount_micro, currency, credited_micro, recovered_micro, "
        "provider_subtype, lifecycle_status, cumulative_refunded, recovery_target, "
        "debit_status, unrecovered_micro, provider_ordering_watermark) SELECT "
        "@workspace_id, @event_id, @kind, @provider, @amount_micro, "
        "@original_payment_ref, @adverse_ref, @occurred_at, @recorded_at, "
        "@payment_amount_micro, @currency, @credited_micro, @recovered_micro, "
        "@provider_subtype, @lifecycle_status, @cumulative_refunded, "
        "@recovery_target, @debit_status, @unrecovered_micro, "
        # FROM UNNEST([1]) is load-bearing, not decoration: GoogleSQL rejects a
        # FROM-less SELECT that carries a WHERE clause ("Query without FROM
        # clause cannot have a WHERE clause"), so the Postgres spelling of this
        # guarded insert fails EVERY call on Spanner. One row when the guard
        # finds nothing, zero when it matches -- the contract the caller reads.
        "@provider_ordering_watermark FROM UNNEST([1]) AS _one WHERE NOT EXISTS ("
        "SELECT 1 FROM tr_trust_event WHERE provider=@provider AND kind=@kind AND ("
        "(@kind='payment' AND @original_payment_ref IS NOT NULL "
        "AND original_payment_ref=@original_payment_ref) OR "
        "(@kind!='payment' AND @adverse_ref IS NOT NULL "
        "AND adverse_ref=@adverse_ref)))",
        params={
            "workspace_id": event.workspace_id,
            "event_id": event.event_id,
            "kind": event.kind,
            "provider": event.provider,
            "amount_micro": event.amount_micro,
            "original_payment_ref": event.original_payment_ref,
            "adverse_ref": event.adverse_ref,
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "payment_amount_micro": event.payment_amount_micro,
            "currency": event.currency,
            "credited_micro": event.credited_micro,
            "recovered_micro": event.recovered_micro,
            "provider_subtype": event.provider_subtype,
            "lifecycle_status": event.lifecycle_status,
            "cumulative_refunded": event.cumulative_refunded,
            "recovery_target": event.recovery_target,
            "debit_status": event.debit_status,
            "unrecovered_micro": event.unrecovered_micro,
            "provider_ordering_watermark": event.provider_ordering_watermark,
        },
        param_types={
            "workspace_id": param_types.STRING,
            "event_id": param_types.STRING,
            "kind": param_types.STRING,
            "provider": param_types.STRING,
            "amount_micro": param_types.INT64,
            "original_payment_ref": param_types.STRING,
            "adverse_ref": param_types.STRING,
            "occurred_at": param_types.TIMESTAMP,
            "recorded_at": param_types.TIMESTAMP,
            "payment_amount_micro": param_types.INT64,
            "currency": param_types.STRING,
            "credited_micro": param_types.INT64,
            "recovered_micro": param_types.INT64,
            "provider_subtype": param_types.STRING,
            "lifecycle_status": param_types.STRING,
            "cumulative_refunded": param_types.INT64,
            "recovery_target": param_types.INT64,
            "debit_status": param_types.STRING,
            "unrecovered_micro": param_types.INT64,
            "provider_ordering_watermark": param_types.STRING,
        },
    )
    return int(inserted) == 1


def _event_from_row(row: list[Any] | tuple[Any, ...]) -> TrustEvent:
    return TrustEvent(*row)


def _read_payment_tx(
    transaction: Any,
    param_types: Any,
    *,
    provider: str,
    original_payment_ref: str,
) -> TrustEvent | None:
    rows = list(
        transaction.execute_sql(
            "SELECT " + ", ".join(TRUST_EVENT_COLUMNS) + " FROM tr_trust_event "  # noqa: S608 - fixed column tuple.
            "WHERE provider=@provider AND kind='payment' "
            "AND original_payment_ref=@original_payment_ref",
            params={
                "provider": provider,
                "original_payment_ref": original_payment_ref,
            },
            param_types={
                "provider": param_types.STRING,
                "original_payment_ref": param_types.STRING,
            },
        )
    )
    if len(rows) > 1:
        raise RuntimeError("payment trust-event dedup invariant violated")
    return None if not rows else _event_from_row(rows[0])


def _sync_principal_recovery_pause_tx(
    transaction: Any,
    param_types: Any,
    *,
    workspace_id: str,
    shard_count: int,
    paused: bool,
    now: dt.datetime,
    read_entity_tx: Callable[..., Any] | None,
    write_entity_tx: Callable[..., Any] | None,
) -> None:
    rows = list(
        transaction.execute_sql(
            "SELECT shard, billing_pause_causes, pause_epoch FROM tr_credit_balance "
            "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count ORDER BY shard",
            params={"pk": workspace_id, "shard_count": shard_count},
            param_types={"pk": param_types.STRING, "shard_count": param_types.INT64},
        )
    )
    if [int(row[0]) for row in rows] != list(range(shard_count)):
        raise RuntimeError("configured tr_credit_balance shard set is incomplete")
    observed = {tuple(sorted(row[1] or ())) for row in rows}
    if len(observed) != 1:
        raise RuntimeError("replicated billing pause causes diverged")
    causes = set(next(iter(observed)))
    if not causes <= TRUST_PAUSE_CAUSES:
        raise RuntimeError("unsupported billing pause cause persisted")
    before = set(causes)
    if paused:
        causes.add("principal_recovery")
    else:
        causes.discard("principal_recovery")
    if causes == before:
        return
    normalized = sorted(causes)
    updated = transaction.execute_update(
        "UPDATE tr_credit_balance SET billing_pause_causes=@causes, "
        "pause_epoch=COALESCE(pause_epoch,0)+1, updated_at=@now "
        "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count",
        params={
            "causes": normalized,
            "now": now,
            "pk": workspace_id,
            "shard_count": shard_count,
        },
        param_types={
            "causes": param_types.Array(param_types.STRING),
            "now": param_types.TIMESTAMP,
            "pk": param_types.STRING,
            "shard_count": param_types.INT64,
        },
    )
    if int(updated) != shard_count:
        raise RuntimeError("billing pause update did not cover every active shard")
    if read_entity_tx is not None and write_entity_tx is not None:
        workspace = read_entity_tx(transaction, "workspace", workspace_id, Workspace)
        if workspace is not None:
            workspace.billing_pause_causes = normalized
            workspace.billing_paused = bool(normalized)
            if paused:
                workspace.billing_pause_reason = "principal_recovery"
            elif not normalized and workspace.billing_pause_reason == "principal_recovery":
                workspace.billing_pause_reason = ""
            write_entity_tx(transaction, "workspace", workspace_id, workspace)
    else:
        reason = "principal_recovery" if paused else ""
        transaction.execute_update(
            "UPDATE tr_entities SET body=TO_JSON_STRING(JSON_SET(PARSE_JSON(body), "
            "'$.billing_paused', @paused, '$.billing_pause_causes', "
            "PARSE_JSON(@causes_json), '$.billing_pause_reason', @reason)), "
            "updated_at=@now WHERE kind='workspace' AND id=@workspace_id",
            params={
                "paused": bool(normalized),
                "causes_json": json.dumps(normalized, separators=(",", ":")),
                "reason": reason,
                "now": now,
                "workspace_id": workspace_id,
            },
            param_types={
                "paused": param_types.BOOL,
                "causes_json": param_types.STRING,
                "reason": param_types.STRING,
                "now": param_types.TIMESTAMP,
                "workspace_id": param_types.STRING,
            },
        )


def absorb_unrecovered_recovery_tx(
    transaction: Any,
    param_types: Any,
    *,
    workspace_id: str,
    amount_micro: int,
    shard_count: int,
    now: dt.datetime,
    read_entity_tx: Callable[..., Any] | None,
    write_entity_tx: Callable[..., Any] | None,
) -> int:
    """Move later credit into oldest payment claims; return amount absorbed."""

    remaining = max(0, int(amount_micro))
    rows = list(
        transaction.execute_sql(
            "SELECT " + ", ".join(TRUST_EVENT_COLUMNS) + " FROM tr_trust_event "  # noqa: S608 - fixed column tuple.
            "WHERE workspace_id=@pk AND kind='payment' AND unrecovered_micro>0 "
            "ORDER BY occurred_at, event_id",
            params={"pk": workspace_id},
            param_types={"pk": param_types.STRING},
        )
    )
    if not rows:
        return 0
    absorbed = 0
    for raw in rows:
        if remaining == 0:
            break
        payment = _event_from_row(raw)
        debt = int(payment.unrecovered_micro or 0)
        take = min(remaining, debt)
        if take == 0:
            continue
        updated = transaction.execute_update(
            "UPDATE tr_trust_event SET recovered_micro=recovered_micro+@amount, "
            "unrecovered_micro=unrecovered_micro-@amount, "
            "debit_status=IF(unrecovered_micro=@amount,'debited','partial') "
            "WHERE workspace_id=@workspace_id AND event_id=@event_id "
            "AND kind='payment' AND unrecovered_micro>=@amount",
            params={
                "amount": take,
                "workspace_id": workspace_id,
                "event_id": payment.event_id,
            },
            param_types={
                "amount": param_types.INT64,
                "workspace_id": param_types.STRING,
                "event_id": param_types.STRING,
            },
        )
        if int(updated) != 1:
            raise RuntimeError("recovery absorption lost its payment claim guard")
        absorbed += take
        remaining -= take
    # A strong sum after our guarded updates is the authoritative cause-clear
    # predicate, including claims that this credit did not fully absorb.
    debt_rows = list(
        transaction.execute_sql(
            "SELECT COALESCE(SUM(unrecovered_micro),0) FROM tr_trust_event "
            "WHERE workspace_id=@pk AND kind='payment'",
            params={"pk": workspace_id},
            param_types={"pk": param_types.STRING},
        )
    )
    still_unrecovered = bool(debt_rows and int(debt_rows[0][0] or 0) > 0)
    _sync_principal_recovery_pause_tx(
        transaction,
        param_types,
        workspace_id=workspace_id,
        shard_count=shard_count,
        paused=still_unrecovered,
        now=now,
        read_entity_tx=read_entity_tx,
        write_entity_tx=write_entity_tx,
    )
    return absorbed


def _debit_available_principal_tx(
    transaction: Any,
    param_types: Any,
    *,
    workspace_id: str,
    shard_count: int,
    amount_micro: int,
    now: dt.datetime,
) -> int:
    rows = list(
        transaction.execute_sql(
            "SELECT shard, total_credits, total_usage, reserved FROM tr_credit_balance "
            "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count ORDER BY shard",
            params={"pk": workspace_id, "shard_count": shard_count},
            param_types={"pk": param_types.STRING, "shard_count": param_types.INT64},
        )
    )
    if [int(row[0]) for row in rows] != list(range(shard_count)):
        raise RuntimeError("configured tr_credit_balance shard set is incomplete")
    remaining = int(amount_micro)
    debited = 0
    for shard, credits, usage, reserved in rows:
        take = min(remaining, max(0, int(credits) - int(usage) - int(reserved)))
        if take == 0:
            continue
        updated = transaction.execute_update(
            "UPDATE tr_credit_balance SET total_credits=total_credits-@amount, "
            "updated_at=@now WHERE workspace_id=@ws AND shard=@shard "
            "AND (total_credits-total_usage-reserved)>=@amount",
            params={
                "amount": take,
                "now": now,
                "ws": workspace_id,
                "shard": int(shard),
            },
            param_types={
                "amount": param_types.INT64,
                "now": param_types.TIMESTAMP,
                "ws": param_types.STRING,
                "shard": param_types.INT64,
            },
        )
        if int(updated) != 1:
            raise RuntimeError("principal recovery debit lost its headroom guard")
        debited += take
        remaining -= take
        if remaining == 0:
            break
    return debited


def apply_adverse_trust_event_tx(
    transaction: Any,
    param_types: Any,
    event: AdverseTrustEvent,
    *,
    now: dt.datetime,
    read_entity_tx: Callable[..., Any],
    write_entity_tx: Callable[..., Any],
) -> AdverseTrustResult | None:
    """Apply fact, latch, recovery, and pause in one local transaction."""

    validate_adverse_event(event)
    payment = _read_payment_tx(
        transaction,
        param_types,
        provider=event.provider,
        original_payment_ref=event.original_payment_ref,
    )
    if payment is None:
        return None
    workspace_id = payment.workspace_id
    existing_rows = list(
        transaction.execute_sql(
            "SELECT " + ", ".join(TRUST_EVENT_COLUMNS) + " FROM tr_trust_event "  # noqa: S608 - fixed column tuple.
            "WHERE provider=@provider AND adverse_ref=@adverse_ref",
            params={"provider": event.provider, "adverse_ref": event.adverse_ref},
            param_types={
                "provider": param_types.STRING,
                "adverse_ref": param_types.STRING,
            },
        )
    )
    if len(existing_rows) > 1:
        raise RuntimeError("adverse lifecycle key is not unique")
    existing = _event_from_row(existing_rows[0]) if existing_rows else None
    if existing is not None and existing.kind != event.kind:
        return AdverseTrustResult(
            "illegal", workspace_id=workspace_id, provider=event.provider
        )
    transition = adverse_transition_outcome(
        kind=event.kind,
        old_status=existing.lifecycle_status if existing else None,
        old_watermark=existing.provider_ordering_watermark if existing else None,
        new_status=event.lifecycle_status,
        new_watermark=event.provider_ordering_watermark,
    )
    if transition != "applied":
        return AdverseTrustResult(
            transition,
            workspace_id,
            int(payment.recovery_target or 0),
            int(payment.recovered_micro or 0),
            int(payment.unrecovered_micro or 0),
            event.provider,
        )
    if existing is None:
        inserted = insert_credit_trust_event(
            transaction,
            param_types,
            TrustEvent(
                workspace_id=workspace_id,
                event_id=event.event_id,
                kind=event.kind,
                provider=event.provider,
                amount_micro=event.amount_micro,
                original_payment_ref=event.original_payment_ref,
                adverse_ref=event.adverse_ref,
                occurred_at=event.occurred_at,
                recorded_at=now,
                payment_amount_micro=payment.payment_amount_micro,
                currency=payment.currency,
                credited_micro=payment.credited_micro,
                recovered_micro=None,
                provider_subtype=event.provider_subtype,
                lifecycle_status=event.lifecycle_status,
                cumulative_refunded=None,
                recovery_target=None,
                debit_status=None,
                unrecovered_micro=None,
                provider_ordering_watermark=event.provider_ordering_watermark,
            ),
        )
        if not inserted:
            raise RuntimeError("adverse insert lost dedup race inside transaction")
        adverse_event_id = event.event_id
    else:
        updated = transaction.execute_update(
            "UPDATE tr_trust_event SET amount_micro=@amount_micro, occurred_at=@occurred_at, "
            "recorded_at=@recorded_at, provider_subtype=@provider_subtype, "
            "lifecycle_status=@lifecycle_status, "
            "provider_ordering_watermark=@provider_ordering_watermark "
            "WHERE workspace_id=@workspace_id AND event_id=@event_id "
            "AND provider=@provider AND adverse_ref=@adverse_ref",
            params={
                "amount_micro": event.amount_micro,
                "occurred_at": event.occurred_at,
                "recorded_at": now,
                "provider_subtype": event.provider_subtype,
                "lifecycle_status": event.lifecycle_status,
                "provider_ordering_watermark": event.provider_ordering_watermark,
                "workspace_id": workspace_id,
                "event_id": existing.event_id,
                "provider": event.provider,
                "adverse_ref": event.adverse_ref,
            },
            param_types={
                "amount_micro": param_types.INT64,
                "occurred_at": param_types.TIMESTAMP,
                "recorded_at": param_types.TIMESTAMP,
                "provider_subtype": param_types.STRING,
                "lifecycle_status": param_types.STRING,
                "provider_ordering_watermark": param_types.STRING,
                "workspace_id": param_types.STRING,
                "event_id": param_types.STRING,
                "provider": param_types.STRING,
                "adverse_ref": param_types.STRING,
            },
        )
        if int(updated) != 1:
            raise RuntimeError("adverse lifecycle update lost its key guard")
        adverse_event_id = existing.event_id

    account = read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
    if account is None:
        raise RuntimeError("payment fact has no credit account")
    shard_count = credit_shard_count(account)
    latched = transaction.execute_update(
        "UPDATE tr_credit_balance SET trust_latched_at=COALESCE(trust_latched_at,@now), "
        "updated_at=@now WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count",
        params={"now": now, "pk": workspace_id, "shard_count": shard_count},
        param_types={
            "now": param_types.TIMESTAMP,
            "pk": param_types.STRING,
            "shard_count": param_types.INT64,
        },
    )
    if int(latched) != shard_count:
        raise RuntimeError("adverse latch did not cover every active shard")
    adverse_rows = list(
        transaction.execute_sql(
            "SELECT " + ", ".join(TRUST_EVENT_COLUMNS) + " FROM tr_trust_event "  # noqa: S608 - fixed column tuple.
            "WHERE workspace_id=@workspace_id AND provider=@provider "
            "AND original_payment_ref=@original_payment_ref AND kind!='payment'",
            params={
                "workspace_id": workspace_id,
                "provider": event.provider,
                "original_payment_ref": event.original_payment_ref,
            },
            param_types={
                "workspace_id": param_types.STRING,
                "provider": param_types.STRING,
                "original_payment_ref": param_types.STRING,
            },
        )
    )
    target, net_refunded = payment_recovery_target(
        payment,
        (_event_from_row(row) for row in adverse_rows),
    )
    old_target = int(payment.recovery_target or 0)
    recovered = int(payment.recovered_micro or 0)
    unrecovered = int(payment.unrecovered_micro or 0)
    if old_target != recovered + unrecovered:
        raise RuntimeError("payment recovery invariant violated before transition")
    delta = target - old_target
    if delta > 0:
        debited = _debit_available_principal_tx(
            transaction,
            param_types,
            workspace_id=workspace_id,
            shard_count=shard_count,
            amount_micro=delta,
            now=now,
        )
        recovered += debited
        unrecovered += delta - debited
    elif delta < 0:
        decrease = -delta
        canceled = min(decrease, unrecovered)
        unrecovered -= canceled
        restore = min(decrease - canceled, recovered)
        recovered -= restore
        if restore:
            for shard, amount in enumerate(distribute_credit_amount(restore, shard_count)):
                if amount and credit_credit_shard(
                    transaction,
                    param_types,
                    workspace_id,
                    amount,
                    shard=shard,
                    now=now,
                ) != 1:
                    raise RuntimeError("principal restoration found a missing shard")
    if target != recovered + unrecovered:
        raise RuntimeError("payment recovery invariant violated after transition")
    debit_status = "debited" if unrecovered == 0 else ("unrecovered" if recovered == 0 else "partial")
    payment_updated = transaction.execute_update(
        "UPDATE tr_trust_event SET recovered_micro=@recovered_micro, "
        "unrecovered_micro=@unrecovered_micro, recovery_target=@recovery_target, "
        "cumulative_refunded=@cumulative_refunded, debit_status=@debit_status "
        "WHERE workspace_id=@workspace_id AND event_id=@event_id AND kind='payment'",
        params={
            "recovered_micro": recovered,
            "unrecovered_micro": unrecovered,
            "recovery_target": target,
            "cumulative_refunded": net_refunded,
            "debit_status": debit_status,
            "workspace_id": workspace_id,
            "event_id": payment.event_id,
        },
        param_types={
            "recovered_micro": param_types.INT64,
            "unrecovered_micro": param_types.INT64,
            "recovery_target": param_types.INT64,
            "cumulative_refunded": param_types.INT64,
            "debit_status": param_types.STRING,
            "workspace_id": param_types.STRING,
            "event_id": param_types.STRING,
        },
    )
    if int(payment_updated) != 1:
        raise RuntimeError("payment recovery update lost its fact guard")
    transaction.execute_update(
        "UPDATE tr_trust_event SET recovery_target=@recovery_target, "
        "cumulative_refunded=@cumulative_refunded, debit_status=@debit_status, "
        "unrecovered_micro=@unrecovered_micro WHERE workspace_id=@workspace_id "
        "AND event_id=@event_id AND kind=@kind",
        params={
            "recovery_target": target,
            "cumulative_refunded": net_refunded,
            "debit_status": debit_status,
            "unrecovered_micro": unrecovered,
            "workspace_id": workspace_id,
            "event_id": adverse_event_id,
            "kind": event.kind,
        },
        param_types={
            "recovery_target": param_types.INT64,
            "cumulative_refunded": param_types.INT64,
            "debit_status": param_types.STRING,
            "unrecovered_micro": param_types.INT64,
            "workspace_id": param_types.STRING,
            "event_id": param_types.STRING,
            "kind": param_types.STRING,
        },
    )
    _sync_principal_recovery_pause_tx(
        transaction,
        param_types,
        workspace_id=workspace_id,
        shard_count=shard_count,
        paused=unrecovered > 0,
        now=now,
        read_entity_tx=read_entity_tx,
        write_entity_tx=write_entity_tx,
    )
    return AdverseTrustResult(
        "applied", workspace_id, target, recovered, unrecovered, event.provider
    )


def insert_trust_inbox_tx(
    transaction: Any,
    param_types: Any,
    event: AdverseTrustEvent,
    *,
    received_at: dt.datetime,
) -> None:
    transaction.execute_update(
        "INSERT INTO tr_trust_inbox (provider, adverse_ref, payload, received_at) "
        # Same GoogleSQL rule as insert_credit_trust_event above.
        "SELECT @provider, @adverse_ref, @payload, @received_at "
        "FROM UNNEST([1]) AS _one WHERE NOT EXISTS ("
        "SELECT 1 FROM tr_trust_inbox WHERE provider=@provider AND adverse_ref=@adverse_ref)",
        params={
            "provider": event.provider,
            "adverse_ref": trust_inbox_reference(event),
            "payload": adverse_event_payload(event),
            "received_at": received_at,
        },
        param_types={
            "provider": param_types.STRING,
            "adverse_ref": param_types.STRING,
            "payload": param_types.STRING,
            "received_at": param_types.TIMESTAMP,
        },
    )


def drain_matching_trust_inbox_tx(
    transaction: Any,
    param_types: Any,
    *,
    provider: str,
    original_payment_ref: str,
    now: dt.datetime,
    read_entity_tx: Callable[..., Any],
    write_entity_tx: Callable[..., Any],
) -> tuple[AdverseTrustResult, ...]:
    rows = list(
        transaction.execute_sql(
            "SELECT provider, adverse_ref, payload, received_at FROM tr_trust_inbox "
            "WHERE provider=@provider ORDER BY received_at, adverse_ref",
            params={"provider": provider},
            param_types={"provider": param_types.STRING},
        )
    )
    results: list[AdverseTrustResult] = []
    for _provider, adverse_ref, payload, _received_at in rows:
        event = adverse_event_from_payload(str(payload))
        if event.original_payment_ref != original_payment_ref:
            continue
        result = apply_adverse_trust_event_tx(
            transaction,
            param_types,
            event,
            now=now,
            read_entity_tx=read_entity_tx,
            write_entity_tx=write_entity_tx,
        )
        if result is None:
            raise RuntimeError("matching inbox row still cannot resolve its payment")
        results.append(result)
        deleted = transaction.execute_update(
            "DELETE FROM tr_trust_inbox WHERE provider=@provider AND adverse_ref=@adverse_ref",
            params={"provider": provider, "adverse_ref": str(adverse_ref)},
            param_types={"provider": param_types.STRING, "adverse_ref": param_types.STRING},
        )
        if int(deleted) != 1:
            raise RuntimeError("trust inbox drain lost its row guard")
    return tuple(results)


def recompute_workspace_trust_tier_tx(
    *,
    run_in_transaction: Callable[[Callable[[Any], int]], int],
    param_types: Any,
    read_entity_tx: Callable[..., Any],
    workspace_id: str,
    qualifying_providers: frozenset[str],
    tier3_min_days: int,
    tier3_min_paid_microdollars: int,
    now: dt.datetime,
) -> int:
    """Rewrite every active shard together; return the effective tier."""

    def txn(transaction: Any) -> int:
        workspace = read_entity_tx(transaction, "workspace", workspace_id, Workspace)
        account = read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
        if workspace is None or account is None:
            raise ValueError("workspace_or_credit_account_not_found")
        owner = read_entity_tx(transaction, "user", workspace.owner_user_id, User)
        owner_status = owner.identity_status if owner is not None else "none"
        override_rows = list(
            transaction.execute_sql(
                "SELECT tier, identity_bypass FROM tr_trust_override "
                "WHERE workspace_id=@pk",
                params={"pk": workspace_id},
                param_types={"pk": param_types.STRING},
            )
        )
        if len(override_rows) > 1:
            raise RuntimeError("trust override primary key invariant violated")
        override_tier = None if not override_rows else int(override_rows[0][0])
        identity_bypass = bool(override_rows and override_rows[0][1])
        event_rows = list(
            transaction.execute_sql(
                "SELECT event_id, kind, provider, amount_micro, original_payment_ref, "
                "adverse_ref, occurred_at, recorded_at, payment_amount_micro, currency, "
                "credited_micro, recovered_micro, provider_subtype, lifecycle_status, "
                "cumulative_refunded, recovery_target, debit_status, unrecovered_micro, "
                "provider_ordering_watermark FROM tr_trust_event "
                "WHERE workspace_id=@pk",
                params={"pk": workspace_id},
                param_types={"pk": param_types.STRING},
            )
        )
        events = [
            TrustEvent(workspace_id, *row)
            for row in event_rows
        ]
        shard_count = credit_shard_count(account)
        shard_rows = list(
            transaction.execute_sql(
                "SELECT shard, trust_tier, trust_latched_at, trust_override_tier "
                "FROM tr_credit_balance WHERE workspace_id=@pk "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"pk": workspace_id, "shard_count": shard_count},
                param_types={
                    "pk": param_types.STRING,
                    "shard_count": param_types.INT64,
                },
            )
        )
        if [int(row[0]) for row in shard_rows] != list(range(shard_count)):
            raise RuntimeError("configured tr_credit_balance shard set is incomplete")
        latch_values = {row[2] for row in shard_rows}
        override_values = {row[3] for row in shard_rows}
        if len(latch_values) != 1 or len(override_values) != 1:
            raise RuntimeError("replicated trust columns diverged")
        if next(iter(override_values)) != override_tier:
            raise RuntimeError("trust override row and replicated shard value diverged")
        decision = compute_trust_tier(
            events,
            owner_identity_status=owner_status,
            trust_latched_at=shard_rows[0][2],
            trust_override_tier=override_tier,
            qualifying_providers=qualifying_providers,
            tier3_min_days=tier3_min_days,
            tier3_min_paid_microdollars=tier3_min_paid_microdollars,
            now=now,
            identity_bypass=identity_bypass,
        )
        updated = transaction.execute_update(
            "UPDATE tr_credit_balance SET trust_tier=@trust_tier, "
            "trust_computed_at=@trust_computed_at "
            "WHERE workspace_id=@pk AND shard>=0 AND shard<@shard_count",
            params={
                "trust_tier": decision.effective_tier,
                "trust_computed_at": now,
                "pk": workspace_id,
                "shard_count": shard_count,
            },
            param_types={
                "trust_tier": param_types.INT64,
                "trust_computed_at": param_types.TIMESTAMP,
                "pk": param_types.STRING,
                "shard_count": param_types.INT64,
            },
        )
        if int(updated) != shard_count:
            raise RuntimeError("trust-tier update did not cover every active shard")
        return decision.effective_tier

    return run_in_transaction(txn)


assert len(CREDIT_BALANCE_TRUST_COLUMNS) == 7
