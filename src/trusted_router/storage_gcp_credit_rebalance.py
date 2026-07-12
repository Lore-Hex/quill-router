"""Lazy, transactionally safe repair of fragmented credit sub-budgets."""

from __future__ import annotations

from typing import Any

from trusted_router.storage_gcp_counter_dml import transfer_credit_budget
from trusted_router.storage_gcp_counters import credit_shard_count
from trusted_router.storage_gcp_io import run_in_transaction_with_retry


class RebalanceOutcome:
    MOVED = "moved"
    NOT_NEEDED = "not_needed"
    INSUFFICIENT = "insufficient"
    INCOMPLETE = "incomplete"


class _RebalanceInvariantError(RuntimeError):
    """Rollback a transfer plan if any guarded DML does not affect one row."""


def rebalance_credit_for_estimate(
    database: Any,
    param_types: Any,
    *,
    workspace_id: str,
    shard_count: int,
    target_shard: int,
    estimate: int,
) -> dict[str, int | str]:
    """Consolidate enough idle headroom onto ``target_shard`` for one hold.

    This is a cold path after a bounded reserve scan rejected every shard. The
    transaction moves only ``total_credits - total_usage - reserved`` and keeps
    the global ``SUM(total_credits)`` byte-for-byte unchanged.
    """
    count = credit_shard_count({"shard_count": shard_count})
    if target_shard < 0 or target_shard >= count:
        raise ValueError("rebalance target shard is outside configured range")
    if estimate <= 0:
        raise ValueError("rebalance estimate must be positive")
    pt = param_types

    def txn(transaction: Any) -> dict[str, int | str]:
        rows = list(
            transaction.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved "
                "FROM tr_credit_balance WHERE workspace_id=@pk "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"pk": workspace_id, "shard_count": count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        observed = [int(row[0]) for row in rows]
        if observed != list(range(count)):
            return {
                "outcome": RebalanceOutcome.INCOMPLETE,
                "moved_micro": 0,
                "target_shard": target_shard,
            }

        headroom: dict[int, int] = {}
        for shard, total_credits, total_usage, reserved in rows:
            available = int(total_credits) - int(total_usage) - int(reserved)
            if available < 0:
                raise _RebalanceInvariantError(
                    f"credit shard {shard} exceeds its sub-budget"
                )
            headroom[int(shard)] = available

        target_available = headroom[target_shard]
        if target_available >= estimate:
            return {
                "outcome": RebalanceOutcome.NOT_NEEDED,
                "moved_micro": 0,
                "target_shard": target_shard,
            }
        if sum(headroom.values()) < estimate:
            return {
                "outcome": RebalanceOutcome.INSUFFICIENT,
                "moved_micro": 0,
                "target_shard": target_shard,
            }

        needed = estimate - target_available
        moved = 0
        donors = sorted(
            (
                (available, shard)
                for shard, available in headroom.items()
                if shard != target_shard and available > 0
            ),
            reverse=True,
        )
        for available, donor_shard in donors:
            amount = min(available, needed)
            if not transfer_credit_budget(
                transaction,
                pt,
                workspace_id,
                amount,
                donor_shard=donor_shard,
                target_shard=target_shard,
            ):
                raise _RebalanceInvariantError(
                    "credit shard changed or disappeared during rebalance"
                )
            moved += amount
            needed -= amount
            if needed == 0:
                break
        if needed != 0:  # pragma: no cover - guarded by global headroom check.
            raise _RebalanceInvariantError("rebalance plan did not satisfy estimate")
        return {
            "outcome": RebalanceOutcome.MOVED,
            "moved_micro": moved,
            "target_shard": target_shard,
        }

    return run_in_transaction_with_retry(database, txn)
