"""Section 4.7's debt rules applied to a workspace's credit rows in Spanner.

`credit_debt` decides what the rows end with; this module reads them and
writes what it decides. Every statement is DML, as the settle and authorize
transactions that call it are (storage_gcp_counter_dml's docstring).

Lock order, as the fast-admission design states it: the caller's own credit
row first, which its own statement already holds; then the workspace's rows in
ascending shard order; key rows after. The caller runs these before touching a
key row.

Each change of a row is conditional on the headroom read in the same
transaction, so a statement that matches no row means the transaction is not
what it read, and it rolls back.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from trusted_router import credit_debt
from trusted_router.storage_gcp_counters import distribute_credit_amount

_READ_ROWS_SQL = (
    "SELECT shard, total_credits - total_usage - reserved, COALESCE(in_debt, FALSE) "
    "FROM tr_credit_balance WHERE workspace_id=@pk ORDER BY shard"
)
_MOVE_SQL = (
    "UPDATE tr_credit_balance SET total_credits = total_credits + @delta, updated_at=@now "
    "WHERE workspace_id=@ws AND shard=@shard "
    "AND (total_credits - total_usage - reserved) = @before"
)
_MARK_SQL = (
    "UPDATE tr_credit_balance SET in_debt=@marked, updated_at=@now "
    "WHERE workspace_id=@ws AND shard>=0 AND shard<@shard_count"
)


class CreditRowsChanged(RuntimeError):
    """A credit row is not what this transaction read: roll the transaction back."""


class CreditRowsIncomplete(RuntimeError):
    """The workspace's credit rows are not shards 0 to n-1, or not the count expected.

    `missing_shard` is the lowest shard that has no row, if one is missing.
    """

    def __init__(self, missing_shard: int | None) -> None:
        super().__init__("configured tr_credit_balance shard set is incomplete")
        self.missing_shard = missing_shard


@dataclass(frozen=True)
class CreditRows:
    """A workspace's credit rows: each row's headroom and mark, in shard order.

    `marked` is whether any row is marked. Every write sets one mark on all of
    them, but a row recreated without the column's value (federated booking's
    zero-credit row) can differ, and the next write makes them agree.
    """

    headroom: tuple[int, ...]
    marks: tuple[bool, ...]

    @property
    def marked(self) -> bool:
        return any(self.marks)


def read_credit_rows(
    transaction: Any, param_types: Any, workspace_id: str, *, shard_count: int | None = None,
) -> CreditRows:
    """Every credit row of the workspace, ascending, with its headroom and mark.

    The rows must be shards 0 to n-1, and n must be `shard_count` if given.
    """

    rows = list(
        transaction.execute_sql(
            _READ_ROWS_SQL,
            params={"pk": workspace_id},
            param_types={"pk": param_types.STRING},
        )
    )
    shards = [int(row[0]) for row in rows]
    if not shards or shards != list(range(len(shards))) or (
        shard_count is not None and len(shards) != shard_count
    ):
        present = set(shards)
        expected = shard_count if shard_count is not None else len(shards) + 1
        missing = next((shard for shard in range(expected) if shard not in present), None)
        raise CreditRowsIncomplete(missing)
    return CreditRows(
        headroom=tuple(int(row[1]) for row in rows),
        marks=tuple(bool(row[2]) for row in rows),
    )


def write_credit_rows(
    transaction: Any,
    param_types: Any,
    workspace_id: str,
    *,
    before: CreditRows,
    headroom: Sequence[int],
    marked: bool,
    now: Any,
) -> None:
    """Move credit so each row's headroom goes from `before` to `headroom`, and set the mark.

    Only credit moves, so a row's change of headroom is its change of
    `total_credits`. Rows are written in ascending shard order; a row whose
    headroom is not what was read stops the transaction.
    """

    for shard, delta in enumerate(credit_debt.credit_deltas(before.headroom, headroom)):
        if delta == 0:
            continue
        count = transaction.execute_update(
            _MOVE_SQL,
            params={
                "delta": delta,
                "now": now,
                "ws": workspace_id,
                "shard": shard,
                "before": before.headroom[shard],
            },
            param_types={
                "delta": param_types.INT64,
                "now": param_types.TIMESTAMP,
                "ws": param_types.STRING,
                "shard": param_types.INT64,
                "before": param_types.INT64,
            },
        )
        if count != 1:
            raise CreditRowsChanged(f"credit row {workspace_id}/{shard} is not what was read")
    if any(mark != marked for mark in before.marks):
        shard_count = len(before.headroom)
        count = transaction.execute_update(
            _MARK_SQL,
            params={"marked": marked, "now": now, "ws": workspace_id, "shard_count": shard_count},
            param_types={
                "marked": param_types.BOOL,
                "now": param_types.TIMESTAMP,
                "ws": param_types.STRING,
                "shard_count": param_types.INT64,
            },
        )
        if count != shard_count:
            raise CreditRowsChanged(f"the debt mark did not reach every credit row of {workspace_id}")


def settle_credit_rows(
    transaction: Any, param_types: Any, workspace_id: str, *, now: Any,
    shard_count: int | None = None,
) -> tuple[CreditRows, credit_debt.Squared]:
    """Square the workspace's rows, and return them as read and what they became.

    If the signed sum is negative, every row is marked. Otherwise every
    negative row is covered from the others and the mark is cleared. The rows
    must be shards 0 to n-1, and n must be `shard_count` if given.
    """

    rows = read_credit_rows(transaction, param_types, workspace_id, shard_count=shard_count)
    result = credit_debt.square(rows.headroom)
    write_credit_rows(
        transaction, param_types, workspace_id,
        before=rows, headroom=result.headroom, marked=result.marked, now=now,
    )
    return rows, result


def cover_or_mark(transaction: Any, param_types: Any, workspace_id: str, *, now: Any) -> credit_debt.Squared:
    """After a write that took money out and may have left a row negative.

    If the signed sum is negative, every row is marked. Otherwise every
    negative row is covered from the others and the mark is cleared.
    """

    return settle_credit_rows(transaction, param_types, workspace_id, now=now)[1]


@dataclass(frozen=True)
class InflowResult:
    """What money coming in did: the rows' mark after, and what claims absorbed."""

    marked: bool
    absorbed: int


def take_inflow(
    transaction: Any,
    param_types: Any,
    workspace_id: str,
    amount: int,
    *,
    landing_shard: int | None,
    absorb: Callable[[int], int] | None,
    now: Any,
    shard_count: int | None = None,
) -> InflowResult:
    """Money coming in: repay the negative rows first, then claims, then the rest.

    `landing_shard` is None for new money, which is on no row yet: what is
    left after repaying and claims is spread over the rows as credits are
    spread today. For a return, it is the row the release freed the money on,
    which already holds it: what repays other rows, and what claims absorb, is
    taken back from that row, and the rest stays there.

    `absorb(offered)` pays unrecovered payment claims from at most `offered`
    and returns what it took. It is offered only what repaid nothing, and only
    if the rows end unmarked. `shard_count`, if given, is the count the caller
    expects the rows to have.
    """

    amount = int(amount)
    if amount < 0:
        raise ValueError("money coming in is not negative")
    rows = read_credit_rows(transaction, param_types, workspace_id, shard_count=shard_count)
    without = list(rows.headroom)
    if landing_shard is not None:
        without[landing_shard] -= amount
    inflow = credit_debt.take_inflow(without, amount)
    left = inflow.left
    absorbed = 0
    if not inflow.marked and left > 0 and absorb is not None:
        absorbed = int(absorb(left))
        if absorbed < 0 or absorbed > left:
            raise RuntimeError("payment claims absorbed more than they were offered")
        left -= absorbed
    after = list(inflow.headroom)
    if landing_shard is None:
        for shard, spread in enumerate(distribute_credit_amount(left, len(after))):
            after[shard] += spread
    else:
        after[landing_shard] += left
    write_credit_rows(
        transaction, param_types, workspace_id,
        before=rows, headroom=after, marked=inflow.marked, now=now,
    )
    return InflowResult(marked=inflow.marked, absorbed=absorbed)
