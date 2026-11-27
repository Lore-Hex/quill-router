"""Lazy, transactionally safe repair of fragmented credit sub-budgets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.credit_refusal_read import HEADROOM, CreditRefusalRead
from trusted_router.storage_gcp_batch_dml import DmlStatement, execute_batch_dml
from trusted_router.storage_gcp_counter_dml import transfer_credit_budget_statements
from trusted_router.storage_gcp_counters import credit_shard_count
from trusted_router.storage_gcp_io import run_in_transaction_with_retry

REBALANCE_COOLDOWN_SECONDS = 0.5


class RebalanceOutcome:
    MOVED = "moved"
    NOT_NEEDED = "not_needed"
    INSUFFICIENT = "insufficient"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class CreditHeadroomPrecheck:
    """Lock-free affordability result plus the directly usable shards, if any."""

    outcome: str
    candidate_shard: int | None = None
    # Every shard that could fund the estimate, in the caller's candidate
    # order. A peer can take one between this snapshot and the retry; the
    # retry tries the rest in the same transaction.
    funded_shards: tuple[int, ...] = ()
    # The snapshot an INSUFFICIENT verdict stands on, for the internal
    # authorize route's X-TR-Credit-Refusal-Read (credit_refusal_read.py).
    credit_read: CreditRefusalRead | None = None


class _RebalanceInvariantError(RuntimeError):
    """Rollback a transfer plan if any guarded DML does not affect one row."""


def credit_headroom_precheck(
    database: Any,
    param_types: Any,
    *,
    workspace_id: str,
    shard_count: int,
    shard_candidates: tuple[int, ...],
    estimate: int,
) -> CreditHeadroomPrecheck:
    """Find a directly usable shard before considering an all-shard rebalance.

    An INSUFFICIENT verdict carries the snapshot it stands on (credit_read).
    The snapshot is multi-use only so that the client keeps its read
    timestamp (storage_gcp_authorize.snapshot_read_timestamp_ns); it makes
    one read."""
    from trusted_router.storage_gcp_authorize import snapshot_read_timestamp_ns

    count = credit_shard_count({"shard_count": shard_count})
    candidates = tuple(shard_candidates)
    if len(candidates) != count or set(candidates) != set(range(count)):
        raise ValueError("credit precheck requires every configured shard exactly once")
    pt = param_types

    with database.snapshot(multi_use=True) as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved "
                "FROM tr_credit_balance WHERE workspace_id=@pk "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"pk": workspace_id, "shard_count": count},
                param_types={"pk": pt.STRING, "shard_count": pt.INT64},
            )
        )
        read_timestamp_ns = snapshot_read_timestamp_ns(snapshot)
    observed = [int(row[0]) for row in rows]
    if observed != list(range(count)):
        return CreditHeadroomPrecheck(RebalanceOutcome.INCOMPLETE)

    headroom: dict[int, int] = {}
    for shard, total_credits, total_usage, reserved in rows:
        available = int(total_credits) - int(total_usage) - int(reserved)
        headroom[int(shard)] = available

    if sum(headroom.values()) < estimate:
        return CreditHeadroomPrecheck(
            RebalanceOutcome.INSUFFICIENT,
            credit_read=CreditRefusalRead.of(HEADROOM, estimate, list(headroom.values()), read_timestamp_ns),
        )
    funded = tuple(candidate for candidate in candidates if headroom[candidate] >= estimate)
    if funded:
        return CreditHeadroomPrecheck(RebalanceOutcome.NOT_NEEDED, funded[0], funded)
    return CreditHeadroomPrecheck(RebalanceOutcome.MOVED)


def rebalance_precheck(
    database: Any,
    param_types: Any,
    *,
    workspace_id: str,
    shard_count: int,
    target_shard: int,
    estimate: int,
) -> str:
    """Compatibility verdict for callers that only need the target outcome."""
    count = credit_shard_count({"shard_count": shard_count})
    if target_shard < 0 or target_shard >= count:
        raise ValueError("rebalance target shard is outside configured range")
    candidates = (target_shard,) + tuple(
        shard for shard in range(count) if shard != target_shard
    )
    return credit_headroom_precheck(
        database,
        param_types,
        workspace_id=workspace_id,
        shard_count=count,
        shard_candidates=candidates,
        estimate=estimate,
    ).outcome


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

    Move only the estimate's shortfall, using the largest donors first. Check
    signed affordability before checking the target, including a peer's
    repair after our precheck. Nonpositive estimates return without reading.
    """
    count = credit_shard_count({"shard_count": shard_count})
    if target_shard < 0 or target_shard >= count:
        raise ValueError("rebalance target shard is outside configured range")
    if estimate <= 0:
        return {
            "outcome": RebalanceOutcome.NOT_NEEDED,
            "moved_micro": 0,
            "target_shard": target_shard,
        }
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
            headroom[int(shard)] = available

        # Feasibility is the SIGNED global available — sum over every shard of
        # (credits - usage - reserved), negatives included. An over-spent shard's
        # debt must count against affordability, otherwise we would transfer
        # enough onto the target for reserve to succeed while the workspace is
        # globally overdrawn (free spend). Donors below still pull only from
        # POSITIVE headroom; when this passes, positive donor headroom is
        # provably >= `needed`, so the plan always completes.
        signed_headroom = sum(headroom.values())
        if signed_headroom < estimate:
            # With what the locked read saw, the read the refusal stands on
            # (the authorize's X-TR-Credit-Refusal-Read): no read timestamp
            # exists before a read-write transaction commits.
            return {
                "outcome": RebalanceOutcome.INSUFFICIENT,
                "moved_micro": 0,
                "target_shard": target_shard,
                "rows": len(headroom),
                "available_micro": signed_headroom,
                "most_micro": max(headroom.values()),
            }

        target_available = headroom[target_shard]
        if target_available >= estimate:
            return {
                "outcome": RebalanceOutcome.NOT_NEEDED,
                "moved_micro": 0,
                "target_shard": target_shard,
            }

        needed = estimate - target_available
        moved = 0
        # Largest donor first, as before. Section 4.7 asks the cold paths for
        # ascending shard order, the lock order of its multi-row writers; this
        # one keeps its allocation instead: each step debits a donor, then
        # credits the target, so its rows are not taken in ascending order
        # either way, and when it crosses a covering write Spanner's wound-wait
        # aborts one of the two, which retries. Ascending donors would change
        # which shards a top-up drains, and the guarded debit, whose repair is
        # a transaction apart from its debit, would then lose the schedule
        # tests/test_credit_rebalance_contention.py checks.
        donors = sorted(
            (
                (available, shard)
                for shard, available in headroom.items()
                if shard != target_shard and available > 0
            ),
            reverse=True,
        )
        statements: list[DmlStatement] = []
        for available, donor_shard in donors:
            amount = min(available, needed)
            statements.extend(transfer_credit_budget_statements(
                pt,
                workspace_id,
                amount,
                donor_shard=donor_shard,
                target_shard=target_shard,
            ))
            moved += amount
            needed -= amount
            if needed == 0:
                break
        if needed != 0:  # pragma: no cover - guarded by global headroom check.
            raise _RebalanceInvariantError("rebalance plan did not satisfy estimate")
        # Keep the same write order and guards, but do not hold the workspace's
        # read locks across two client/server round trips per donor.
        execute_batch_dml(transaction, statements, [(1,)] * len(statements))
        return {
            "outcome": RebalanceOutcome.MOVED,
            "mode": "topped_up",
            "moved_micro": moved,
            "target_shard": target_shard,
        }

    return run_in_transaction_with_retry(database, txn)
