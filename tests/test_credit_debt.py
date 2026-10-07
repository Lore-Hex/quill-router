"""The debt rules against `proofs/CreditDebt.tla`.

`credit_debt` is the shadow of `CreditDebt` (proofs/manifest.toml). These
tests transliterate the spec's `Cover`, `Squared`, `Distribute` and `Inflow`
into an oracle, compare the module with it over every small case, and check
the spec's properties on the module's results: `MarkMeansDebt`, no row
negative unless every row is marked, and `RepaysDebtFirst`.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence

import pytest

from trusted_router.credit_debt import (
    Inflow,
    Squared,
    cover,
    credit_deltas,
    repay,
    square,
    take_inflow,
)

# The oracle: the spec's operators, line by line, on each row's headroom.


def _spec_cover(h: Sequence[int]) -> tuple[int, ...]:
    if all(value >= 0 for value in h):
        return tuple(h)
    neg = min(s for s, value in enumerate(h) if value < 0)
    pos = min(s for s, value in enumerate(h) if value > 0)
    x = -h[neg] if -h[neg] < h[pos] else h[pos]
    rows = list(h)
    rows[neg] += x
    rows[pos] -= x
    return _spec_cover(rows)


def _spec_squared(h: Sequence[int]) -> tuple[tuple[int, ...], bool]:
    if sum(h) < 0:
        return tuple(h), True
    return _spec_cover(h), False


def _spec_distribute(h: Sequence[int], x: int, s: int) -> tuple[int, ...]:
    rows = list(h)
    if x == 0:
        return tuple(rows)
    if all(value >= 0 for value in rows):
        rows[s] += x
        return tuple(rows)
    neg = min(t for t, value in enumerate(rows) if value < 0)
    y = x if x < -rows[neg] else -rows[neg]
    rows[neg] += y
    return _spec_distribute(rows, x - y, s)


def _spec_inflow(h_with_money: Sequence[int], s: int, x: int) -> tuple[tuple[int, ...], bool]:
    """`Inflow`: the money x is already on row s; take it off and distribute it."""

    without = list(h_with_money)
    without[s] -= x
    return _spec_squared(_spec_distribute(without, x, s))


def _rows(shards: int, low: int, high: int) -> Iterator[tuple[int, ...]]:
    return itertools.product(range(low, high + 1), repeat=shards)


SMALL = [(1, -6, 6), (2, -4, 4), (3, -3, 3), (4, -2, 2)]


def _cases() -> Iterator[tuple[tuple[int, ...], int, int]]:
    for shards, low, high in SMALL:
        for rows in _rows(shards, low, high):
            for amount in range(0, 2 * high + 2):
                for landing in range(shards):
                    yield rows, amount, landing


# The module against the oracle.


def test_square_is_the_spec_squared_on_every_small_case() -> None:
    for shards, low, high in SMALL:
        for rows in _rows(shards, low, high):
            result = square(rows)
            assert (result.headroom, result.marked) == _spec_squared(rows), rows


def test_take_inflow_is_the_spec_inflow_on_every_small_case() -> None:
    for rows, amount, landing in _cases():
        result = take_inflow(rows, amount)
        # The spec puts what is left on the row the money landed on.
        final = list(result.headroom)
        final[landing] += result.left
        with_money = list(rows)
        with_money[landing] += amount
        assert (tuple(final), result.marked) == _spec_inflow(with_money, landing, amount), (
            rows, amount, landing,
        )


# The spec's properties, on the module's results.


def test_marked_means_debt_and_unmarked_means_no_negative_row() -> None:
    for rows, amount, landing in _cases():
        result = take_inflow(rows, amount)
        final = list(result.headroom)
        final[landing] += result.left
        if result.marked:
            assert sum(final) < 0, (rows, amount)
            assert result.left == 0, (rows, amount)
        else:
            assert min(final) >= 0, (rows, amount)


def test_money_coming_in_repays_debt_first() -> None:
    """`RepaysDebtFirst`: while a row is still negative after the step, the step
    raised only rows that were negative, each at most to zero, and none while a
    lower row stays negative."""

    for rows, amount, landing in _cases():
        result = take_inflow(rows, amount)
        after = list(result.headroom)
        after[landing] += result.left
        if any(value < 0 for value in after):
            for shard, (old, new) in enumerate(zip(rows, after, strict=True)):
                if new > old:
                    assert old < 0 and new <= 0, (rows, amount, landing)
                    assert all(after[lower] >= 0 for lower in range(shard)), (rows, amount, landing)


def test_inflow_and_square_make_and_lose_no_money() -> None:
    for rows, amount, _landing in _cases():
        result = take_inflow(rows, amount)
        assert sum(result.headroom) + result.left == sum(rows) + amount
    for shards, low, high in SMALL:
        for rows in _rows(shards, low, high):
            assert sum(square(rows).headroom) == sum(rows)


def test_covering_moves_credit_only_from_positive_rows_to_negative_ones() -> None:
    for shards, low, high in SMALL:
        for rows in _rows(shards, low, high):
            if sum(rows) < 0:
                continue
            for old, new in zip(rows, cover(rows), strict=True):
                if old < 0:
                    assert old <= new <= 0
                elif old > 0:
                    assert 0 <= new <= old
                else:
                    assert new == 0


# The plan's examples (debt plan, "Two primitives, one ordering").


def test_a_payment_repays_the_lowest_negative_row_and_the_rows_stay_marked() -> None:
    assert take_inflow((-100, 20), 40) == Inflow(headroom=(-60, 20), marked=True, left=0)


def test_a_return_on_a_positive_row_repays_the_negative_row() -> None:
    # Shard 1 had headroom 20 before the release freed 10 on it.
    result = take_inflow((-100, 20), 10)
    assert result == Inflow(headroom=(-90, 20), marked=True, left=0)
    # Shard 1 gives back what the release added to it.
    assert credit_deltas((-100, 30), result.headroom) == (10, -10)


def test_a_return_repays_a_lower_row_before_its_own() -> None:
    # Shard 1 had headroom -10 before the release freed 5 on it.
    result = take_inflow((-10, -10), 5)
    assert result == Inflow(headroom=(-5, -10), marked=True, left=0)
    assert credit_deltas((-10, -5), result.headroom) == (5, -5)


def test_shard_debt_comes_before_payment_claims() -> None:
    # Headroom -50, a claim of 100 and a payment of 50: nothing is left for the claim.
    assert take_inflow((-50,), 50) == Inflow(headroom=(0,), marked=False, left=0)


def test_repaying_clears_the_mark_before_the_rest_is_offered_to_claims() -> None:
    # Headroom -50, marked, a claim of 50 and a payment of 100: the mark clears
    # and 50 is left to offer the claim.
    assert take_inflow((-50,), 100) == Inflow(headroom=(0,), marked=False, left=50)


def test_a_marked_workspace_whose_sum_is_no_longer_negative_is_covered_and_cleared() -> None:
    assert square((-30, 50)) == Squared(headroom=(0, 20), marked=False)
    assert square((-30, 20)) == Squared(headroom=(-30, 20), marked=True)


def test_covering_takes_from_the_lowest_positive_row_first() -> None:
    assert cover((-5, 3, 4)) == (0, 0, 2)
    assert cover((2, -3, 2, -1)) == (0, 0, 0, 0)


def test_repay_stops_when_the_money_runs_out() -> None:
    assert repay((-3, -3, 5), 4) == ((0, -2, 5), 0)
    assert repay((-3, 1, -2), 10) == ((0, 1, 0), 5)


@pytest.mark.parametrize(
    "call",
    [
        lambda: take_inflow((), 1),
        lambda: take_inflow((1,), -1),
        lambda: repay((0,), -1),
        lambda: cover((-2, 1)),
        lambda: square(()),
    ],
)
def test_impossible_inputs_are_refused(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        call()


def test_credit_deltas_are_the_change_of_each_row() -> None:
    assert credit_deltas((1, -2, 3), (0, 0, 2)) == (-1, 2, -1)
    with pytest.raises(ValueError):
        credit_deltas((1, 2), (1,))
