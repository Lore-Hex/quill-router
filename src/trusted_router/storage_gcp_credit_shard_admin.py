"""Fail-closed operator primitives for splitting and consolidating credit rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    credit_shard_count,
    distribute_credit_amount,
)
from trusted_router.storage_models import CreditAccount, Reservation, Workspace

_RESHARD_COLUMNS = (
    "workspace_id",
    "shard",
    "total_credits",
    "total_usage",
    "reserved",
    "source_updated_at",
    "updated_at",
)


@dataclass
class CreditReshardResult:
    workspace_id: str
    target_shard_count: int
    current_shard_count: int | None = None
    total_credits_micro: int | None = None
    total_usage_micro: int | None = None
    reserved_micro: int | None = None
    typed_open_reservations: int = 0
    legacy_open_reservations: int = 0
    reasons: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def ready(self) -> bool:
        return not self.reasons


def _typed_state(
    store: Any,
    workspace_id: str,
    shard_count: int,
) -> tuple[list[list[Any]], int]:
    pt = store._param_types
    with store._database.snapshot(multi_use=True) as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved "
                "FROM tr_credit_balance WHERE workspace_id=@pk "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"pk": workspace_id, "shard_count": shard_count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        open_reservations = int(
            list(
                snapshot.execute_sql(
                    "SELECT COUNT(*) FROM tr_reservation "
                    "WHERE workspace_id=@ws AND settled = false",
                    params={"ws": workspace_id},
                    param_types={"ws": pt.STRING},
                )
            )[0][0]
        )
    return rows, open_reservations


def inspect_credit_reshard(
    store: Any,
    workspace_id: str,
    target_shard_count: int,
) -> CreditReshardResult:
    """Read-only readiness check for a paused, fully drained reshard."""
    target_count = credit_shard_count({"shard_count": target_shard_count})
    result = CreditReshardResult(
        workspace_id=workspace_id,
        target_shard_count=target_count,
    )
    workspace = store.get_workspace(workspace_id)
    account = store.get_credit_account(workspace_id)
    if workspace is None:
        result.reasons.append("workspace not found")
    elif not workspace.billing_paused:
        result.reasons.append("workspace not billing-paused")
    if account is None:
        result.reasons.append("credit account not found")
        return result

    current_count = credit_shard_count(account)
    result.current_shard_count = current_count
    rows, typed_open = _typed_state(store, workspace_id, current_count)
    result.typed_open_reservations = typed_open
    result.legacy_open_reservations = sum(
        1
        for reservation in store._list_entities("reservation", cls=Reservation)
        if reservation.workspace_id == workspace_id and not reservation.settled
    )
    observed = [int(row[0]) for row in rows]
    if observed != list(range(current_count)):
        result.reasons.append("configured typed credit shard set is incomplete")
        return result

    total_credits = sum(int(row[1]) for row in rows)
    total_usage = sum(int(row[2]) for row in rows)
    reserved = sum(int(row[3]) for row in rows)
    result.total_credits_micro = total_credits
    result.total_usage_micro = total_usage
    result.reserved_micro = reserved
    if any(int(row[2]) < 0 or int(row[3]) < 0 for row in rows):
        result.reasons.append("typed credit shard has a negative counter")
    if any(int(row[2]) + int(row[3]) > int(row[1]) for row in rows):
        result.reasons.append("typed credit shard exceeds its sub-budget")
    if reserved != 0:
        result.reasons.append(f"typed credit has reserved={reserved}; wait for drain")
    if typed_open != 0:
        result.reasons.append(f"{typed_open} open typed reservations; wait for drain")
    if result.legacy_open_reservations != 0:
        result.reasons.append(
            f"{result.legacy_open_reservations} open legacy reservations; wait for drain"
        )
    return result


def reshard_credit_account(
    store: Any,
    workspace_id: str,
    target_shard_count: int,
    *,
    apply: bool = False,
) -> CreditReshardResult:
    """Atomically repartition a paused and drained workspace's credit ledger.

    Both splitting and consolidation use this function. A dry run never writes.
    The JSON shard configuration and every typed row commit in one transaction.
    """
    status = inspect_credit_reshard(store, workspace_id, target_shard_count)
    if not status.ready or not apply:
        return status
    assert status.current_shard_count is not None
    if status.current_shard_count == status.target_shard_count:
        return status

    pt = store._param_types
    target_count = status.target_shard_count

    def txn(transaction: Any) -> dict[str, int] | None:
        workspace = store._read_entity_tx(transaction, "workspace", workspace_id, Workspace)
        account = store._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
        if workspace is None or not workspace.billing_paused or account is None:
            return None
        current_count = credit_shard_count(account)
        rows = list(
            transaction.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved "
                "FROM tr_credit_balance WHERE workspace_id=@pk "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"pk": workspace_id, "shard_count": current_count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        observed = [int(row[0]) for row in rows]
        if observed != list(range(current_count)):
            return None
        open_typed = int(
            list(
                transaction.execute_sql(
                    "SELECT COUNT(*) FROM tr_reservation "
                    "WHERE workspace_id=@ws AND settled = false",
                    params={"ws": workspace_id},
                    param_types={"ws": pt.STRING},
                )
            )[0][0]
        )
        total_credits = sum(int(row[1]) for row in rows)
        total_usage = sum(int(row[2]) for row in rows)
        reserved = sum(int(row[3]) for row in rows)
        if (
            open_typed != 0
            or reserved != 0
            or any(int(row[2]) < 0 or int(row[3]) < 0 for row in rows)
            or any(int(row[2]) + int(row[3]) > int(row[1]) for row in rows)
        ):
            return None

        credit_parts = distribute_credit_amount(total_credits, target_count)
        usage_parts = distribute_credit_amount(total_usage, target_count)
        if any(usage > credit for usage, credit in zip(usage_parts, credit_parts, strict=True)):
            return None  # defensive; global usage<=credits should make this impossible.
        commit_timestamp = store._spanner.COMMIT_TIMESTAMP
        transaction.insert_or_update(
            table=CREDIT_BALANCE_TABLE,
            columns=_RESHARD_COLUMNS,
            values=[
                (
                    workspace_id,
                    shard,
                    credit_parts[shard],
                    usage_parts[shard],
                    0,
                    commit_timestamp,
                    commit_timestamp,
                )
                for shard in range(target_count)
            ],
        )
        if current_count > target_count:
            transaction.delete(
                CREDIT_BALANCE_TABLE,
                store._spanner.KeySet(
                    keys=[
                        (workspace_id, shard)
                        for shard in range(target_count, current_count)
                    ]
                ),
            )

        account.shard_count = target_count
        # Deliberately bypass _write_entity_tx: this admin transaction already
        # owns the exact typed-row mutations above.
        transaction.insert_or_update(
            table=store.entity_table,
            columns=("kind", "id", "body", "updated_at"),
            values=[
                (
                    "credit",
                    workspace_id,
                    json_body(account),
                    commit_timestamp,
                )
            ],
        )
        return {
            "current_count": current_count,
            "total_credits": total_credits,
            "total_usage": total_usage,
        }

    changed = store._run_in_transaction(txn)
    if changed is None:
        status.reasons.append(
            "atomic reshard preconditions changed; workspace remains paused"
        )
        return status
    store._credit_shard_counts.invalidate(workspace_id)
    verified = inspect_credit_reshard(store, workspace_id, target_count)
    if not verified.ready:
        verified.reasons.append("post-commit reshard verification failed")
        return verified
    verified.applied = True
    return verified
