"""The debt rules: what a write to a workspace's credit rows must end with.

Fast-admission design, section 4.7, as `proofs/CreditDebt.tla` states them
(`Cover`, `Squared`, `Distribute` and `Inflow`). A workspace's credit is
spread over shard rows of `tr_credit_balance`; a row's headroom is its credit
less its usage and its reservations, and the workspace's signed sum is the sum
of its rows' headroom.

- No row is negative unless every row is marked, and the rows are marked only
  while the signed sum is negative. A marked row refuses reservations.
- Money coming in repays the negative rows first, lowest shard first, each at
  most to zero.
- A negative row is covered from the others: the lowest negative row from the
  lowest positive row first.

These functions only compute. The storage code reads the rows, calls them and
writes what they return. Only credit moves, so a row's change of headroom is a
change of its `total_credits`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Squared:
    """What the rows end with: each row's headroom, in shard order, and the mark."""

    headroom: tuple[int, ...]
    marked: bool


@dataclass(frozen=True)
class Inflow:
    """Money coming in, after it has repaid what it can and the rows are squared.

    `left` is the part of the money that repaid nothing, and is not in
    `headroom`. It is 0 when the rows end marked. The caller offers it to
    unrecovered payment claims, then spreads what is left of new money over
    the rows, or leaves what is left of a return on the row that freed it.
    """

    headroom: tuple[int, ...]
    marked: bool
    left: int


def _rows(headroom: Sequence[int]) -> list[int]:
    rows = [int(value) for value in headroom]
    if not rows:
        raise ValueError("a workspace has at least one credit row")
    return rows


def cover(headroom: Sequence[int]) -> tuple[int, ...]:
    """Move headroom from positive rows to negative ones until none is negative.

    The lowest negative row takes from the lowest positive row first, as
    `Cover` does. The signed sum must not be negative.
    """

    rows = _rows(headroom)
    if sum(rows) < 0:
        raise ValueError("rows whose signed sum is negative are marked, not covered")
    while True:
        negative = next((shard for shard, value in enumerate(rows) if value < 0), None)
        if negative is None:
            return tuple(rows)
        positive = next(shard for shard, value in enumerate(rows) if value > 0)
        moved = min(-rows[negative], rows[positive])
        rows[negative] += moved
        rows[positive] -= moved


def square(headroom: Sequence[int]) -> Squared:
    """What any write to the rows ends with (`Squared`).

    If the signed sum is negative, every row is marked and nothing moves.
    Otherwise every negative row is covered and no row is marked.
    """

    rows = _rows(headroom)
    if sum(rows) < 0:
        return Squared(headroom=tuple(rows), marked=True)
    return Squared(headroom=cover(rows), marked=False)


def repay(headroom: Sequence[int], amount: int) -> tuple[tuple[int, ...], int]:
    """Spend `amount` on the negative rows, lowest first, each at most to zero.

    Returns the rows after and what is left of the amount (`Distribute`,
    before it puts what is left on a row).
    """

    rows = _rows(headroom)
    left = int(amount)
    if left < 0:
        raise ValueError("money coming in is not negative")
    for shard, value in enumerate(rows):
        if left == 0:
            break
        if value < 0:
            paid = min(left, -value)
            rows[shard] += paid
            left -= paid
    return tuple(rows), left


def take_inflow(headroom: Sequence[int], amount: int) -> Inflow:
    """Money coming in (`Inflow`): repay the negative rows first, then square.

    `headroom` is each row's headroom without the money. For a return, that
    is its own row's headroom before the release freed it.
    """

    repaid, left = repay(headroom, amount)
    if sum(repaid) < 0:
        # The money ran out with a row still negative: all of it repaid.
        return Inflow(headroom=repaid, marked=True, left=0)
    return Inflow(headroom=cover(repaid), marked=False, left=left)


def credit_deltas(before: Sequence[int], after: Sequence[int]) -> tuple[int, ...]:
    """Each row's change of `total_credits` that takes its headroom from before to after."""

    return tuple(int(new) - int(old) for old, new in zip(before, after, strict=True))
