from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from trusted_router.money import (
    dollars_to_cents,
    dollars_to_microdollars,
    microdollars_per_million_tokens_to_token_decimal,
    microdollars_to_decimal,
    token_cost_microdollars,
)


def test_microdollars_format_without_float_rounding() -> None:
    assert microdollars_to_decimal(1) == "0.000001"
    assert microdollars_to_decimal(10_000) == "0.01"
    assert microdollars_to_decimal(1_000_000) == "1"
    assert microdollars_to_decimal(1_234_567) == "1.234567"


def test_decimal_inputs_convert_to_integer_microdollars_and_cents() -> None:
    assert dollars_to_microdollars("0.000001") == 1
    assert dollars_to_microdollars("25.123456") == 25_123_456
    assert dollars_to_cents("25.125") == 2513


def test_per_million_token_prices_support_one_cent_discounts() -> None:
    assert microdollars_per_million_tokens_to_token_decimal(4_990_000) == "0.00000499"
    assert token_cost_microdollars(1_000_000, 4_990_000) == 4_990_000
    assert token_cost_microdollars(20, 4_990_000) == 100


@given(st.integers(min_value=-10_000_000_000, max_value=10_000_000_000))
def test_microdollar_decimal_representation_round_trips_without_float(value: int) -> None:
    assert dollars_to_microdollars(microdollars_to_decimal(value)) == value


@given(
    st.integers(min_value=0, max_value=10_000_000),
    st.integers(min_value=0, max_value=50_000_000),
)
def test_token_cost_is_monotonic_and_integer(tokens: int, price: int) -> None:
    cost = token_cost_microdollars(tokens, price)
    assert isinstance(cost, int)
    assert cost >= 0
    assert token_cost_microdollars(tokens + 1, price) >= cost
