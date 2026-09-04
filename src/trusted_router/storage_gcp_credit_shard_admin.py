"""Fail-closed operator primitives for splitting and consolidating credit rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    CREDIT_BALANCE_TRUST_COLUMNS,
    credit_shard_count,
    distribute_credit_amount,
)
from trusted_router.storage_gcp_legacy_reservations import (
    legacy_reservation_snapshot,
)
from trusted_router.storage_models import (
    CreditAccount,
    User,
    Workspace,
    workspace_billing_paused,
)
from trusted_router.trust_ownership import require_owner_trust_budget

_RESHARD_COLUMNS = (
    "workspace_id",
    "shard",
    "total_credits",
    "total_usage",
    "reserved",
    *CREDIT_BALANCE_TRUST_COLUMNS,
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
    stale_legacy_reservations_ignored: int = 0
    reasons: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def ready(self) -> bool:
        return not self.reasons

    @property
    def at_target(self) -> bool:
        """Whether the ledger is ACTUALLY partitioned at the requested count.

        Deliberately separate from `ready`, which only means "nothing blocks a
        reshard". Before a reshard the current count differs from the target by
        definition, so `reshard_credit_account`'s preflight must not require
        this. Verification after the fact must: a drained, paused, healthy
        one-shard workspace inspected with a target of 16 is `ready` while still
        at one shard, and unpausing on `ready` alone would claim a transition
        that never happened.
        """
        return self.current_shard_count == self.target_shard_count


def _typed_state(
    store: Any,
    workspace_id: str,
    shard_count: int,
) -> tuple[list[list[Any]], int, dict[int, int]]:
    pt = store._param_types
    with store._database.snapshot(multi_use=True) as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved, "
                + ", ".join(CREDIT_BALANCE_TRUST_COLUMNS)
                + " "
                "FROM tr_credit_balance WHERE workspace_id=@pk "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"pk": workspace_id, "shard_count": shard_count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        hold_rows = list(
            snapshot.execute_sql(
                "SELECT credit_shard, ws_shard, COUNT(*), "
                "COALESCE(SUM(credit_reserved_micro), 0) "
                "FROM tr_reservation WHERE workspace_id=@ws AND settled=false "
                "GROUP BY credit_shard, ws_shard",
                params={"ws": workspace_id},
                param_types={"ws": pt.STRING},
            )
        )
    open_reservations = 0
    reserved_by_shard: dict[int, int] = {}
    for credit_shard, ws_shard, count, reserved in hold_rows:
        shard = int(credit_shard if credit_shard is not None else (ws_shard or 0))
        open_reservations += int(count)
        reserved_by_shard[shard] = reserved_by_shard.get(shard, 0) + int(
            reserved or 0
        )
    return rows, open_reservations, reserved_by_shard


def _validate_open_holds(
    rows: list[list[Any]],
    reserved_by_shard: dict[int, int],
) -> list[str]:
    reasons: list[str] = []
    observed = {int(row[0]): int(row[3]) for row in rows}
    unknown = sorted(set(reserved_by_shard) - set(observed))
    if unknown:
        reasons.append(f"open typed credit holds reference unknown shards {unknown}")
    for shard, reserved in observed.items():
        held = reserved_by_shard.get(shard, 0)
        if reserved != held:
            reasons.append(
                f"typed credit shard {shard} reserved={reserved} "
                f"but open holds={held}"
            )
    return reasons


def _trust_values(row: list[Any]) -> tuple[Any, ...]:
    return tuple(row[4 : 4 + len(CREDIT_BALANCE_TRUST_COLUMNS)])


def _normalized_trust_values(row: list[Any]) -> tuple[Any, ...]:
    values = list(_trust_values(row))
    causes_index = CREDIT_BALANCE_TRUST_COLUMNS.index("billing_pause_causes")
    if values[causes_index] is not None:
        values[causes_index] = tuple(values[causes_index])
    return tuple(values)


def _validate_trust_replication(rows: list[list[Any]]) -> list[str]:
    if not rows:
        return []
    expected = _normalized_trust_values(rows[0])
    if any(_normalized_trust_values(row) != expected for row in rows[1:]):
        return ["typed credit shards have divergent replicated trust columns"]
    return []


def inspect_credit_reshard(
    store: Any,
    workspace_id: str,
    target_shard_count: int,
    *,
    preserve_open_holds: bool = False,
) -> CreditReshardResult:
    """Read-only readiness check for a paused reshard.

    The default requires a fully drained ledger. The explicit online mode only
    permits splits and verifies that each typed reserved counter exactly matches
    the open holds that will continue settling against that shard.
    """
    target_count = credit_shard_count({"shard_count": target_shard_count})
    result = CreditReshardResult(
        workspace_id=workspace_id,
        target_shard_count=target_count,
    )
    workspace = store.get_workspace(workspace_id)
    account = store.get_credit_account(workspace_id)
    if workspace is None:
        result.reasons.append("workspace not found")
    elif not workspace_billing_paused(workspace):
        result.reasons.append("workspace not billing-paused")
    if account is None:
        result.reasons.append("credit account not found")
        return result

    current_count = credit_shard_count(account)
    result.current_shard_count = current_count
    rows, typed_open, reserved_by_shard = _typed_state(
        store, workspace_id, current_count
    )
    result.typed_open_reservations = typed_open
    legacy = legacy_reservation_snapshot(store)
    result.legacy_open_reservations = legacy.live_by_workspace.get(workspace_id, 0)
    result.stale_legacy_reservations_ignored = legacy.stale_by_workspace.get(
        workspace_id, 0
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
    result.reasons.extend(_validate_open_holds(rows, reserved_by_shard))
    result.reasons.extend(_validate_trust_replication(rows))
    if any(int(row[2]) < 0 or int(row[3]) < 0 for row in rows):
        result.reasons.append("typed credit shard has a negative counter")
    if any(int(row[2]) + int(row[3]) > int(row[1]) for row in rows):
        result.reasons.append("typed credit shard exceeds its sub-budget")
    if preserve_open_holds:
        if target_count < current_count:
            result.reasons.append(
                "hold-preserving reshard only supports increasing the shard count"
            )
    else:
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
    preserve_open_holds: bool = False,
) -> CreditReshardResult:
    """Atomically repartition a paused workspace's credit ledger.

    Both splitting and consolidation use this function. A dry run never writes.
    The JSON shard configuration and every typed row commit in one transaction.
    ``preserve_open_holds`` is an opt-in split-only path: existing usage and
    reserved counters remain on their original shard IDs, while only free
    capacity is distributed to new shards. Existing reservations can therefore
    settle normally after the split.
    """
    status = inspect_credit_reshard(
        store,
        workspace_id,
        target_shard_count,
        preserve_open_holds=preserve_open_holds,
    )
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
        if workspace is None or not workspace_billing_paused(workspace) or account is None:
            return None
        current_count = credit_shard_count(account)
        owned_workspace_ids, owned_shard_counts = store._owner_shard_counts_tx(
            transaction, workspace.owner_user_id
        )
        if workspace_id not in owned_workspace_ids:
            # A slice-1a/1b' workspace can legitimately predate the inventory
            # backfill. Repair that legacy gap on touch so reshard remains
            # available while the fleet-wide backfill drains, and so the
            # resulting fan-out is represented before this transaction commits.
            store._insert_owner_inventory_tx(
                transaction, workspace.owner_user_id, workspace_id
            )
            owner = store._read_entity_tx(
                transaction, "user", workspace.owner_user_id, User
            )
            if owner is not None:
                owner.owner_workspace_count = len(owned_workspace_ids) + 1
                store._write_entity_tx(transaction, "user", owner.id, owner)
            owned_workspace_ids.append(workspace_id)
            owned_shard_counts.append(current_count)
        if target_count > current_count:
            require_owner_trust_budget(
                [
                    target_count if owned_id == workspace_id else shard_count
                    for owned_id, shard_count in zip(
                        owned_workspace_ids, owned_shard_counts, strict=True
                    )
                ]
            )
        rows = list(
            transaction.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved, "
                + ", ".join(CREDIT_BALANCE_TRUST_COLUMNS)
                + " "
                "FROM tr_credit_balance WHERE workspace_id=@pk "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"pk": workspace_id, "shard_count": current_count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        observed = [int(row[0]) for row in rows]
        if observed != list(range(current_count)):
            return None
        total_credits = sum(int(row[1]) for row in rows)
        total_usage = sum(int(row[2]) for row in rows)
        reserved = sum(int(row[3]) for row in rows)
        invalid_counters = (
            any(int(row[2]) < 0 or int(row[3]) < 0 for row in rows)
            or any(int(row[2]) + int(row[3]) > int(row[1]) for row in rows)
        )
        if invalid_counters:
            return None
        if _validate_trust_replication(rows):
            return None
        trust_values = _trust_values(rows[0])

        if preserve_open_holds:
            if target_count < current_count:
                return None
            usage_parts = [int(row[2]) for row in rows] + [0] * (
                target_count - current_count
            )
            reserved_parts = [int(row[3]) for row in rows] + [0] * (
                target_count - current_count
            )
            committed = sum(usage_parts) + sum(reserved_parts)
            if committed > total_credits:
                return None
            free_parts = distribute_credit_amount(
                total_credits - committed, target_count
            )
            credit_parts = [
                usage_parts[shard] + reserved_parts[shard] + free_parts[shard]
                for shard in range(target_count)
            ]
        else:
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
            if open_typed != 0 or reserved != 0:
                return None
            credit_parts = list(distribute_credit_amount(total_credits, target_count))
            usage_parts = list(distribute_credit_amount(total_usage, target_count))
            reserved_parts = [0] * target_count
            if any(
                usage > credit
                for usage, credit in zip(
                    usage_parts, credit_parts, strict=True
                )
            ):
                return None
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
                    reserved_parts[shard],
                    *trust_values,
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
    verified = inspect_credit_reshard(
        store,
        workspace_id,
        target_count,
        preserve_open_holds=preserve_open_holds,
    )
    if not verified.ready:
        verified.reasons.append("post-commit reshard verification failed")
        return verified
    verified.applied = True
    return verified
