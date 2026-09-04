"""Typed Spanner trust facts and the all-shard trust-tier computation."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TRUST_COLUMNS, credit_shard_count
from trusted_router.storage_models import CreditAccount, TrustEvent, User, Workspace
from trusted_router.trust_tiers import compute_trust_tier

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
        "@provider_ordering_watermark WHERE NOT EXISTS ("
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
        decision = compute_trust_tier(
            events,
            owner_identity_status=owner_status,
            trust_latched_at=shard_rows[0][2],
            trust_override_tier=shard_rows[0][3],
            qualifying_providers=qualifying_providers,
            tier3_min_days=tier3_min_days,
            tier3_min_paid_microdollars=tier3_min_paid_microdollars,
            now=now,
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
