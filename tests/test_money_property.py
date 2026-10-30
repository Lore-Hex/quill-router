"""Property tests for money math.

Float arithmetic on currency is a perennial off-by-one source. Hypothesis
generates random integers and checks that the round-trip identities and
formatting invariants hold across the whole int range we care about.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from trusted_router.money import (
    MAX_CHECKOUT_DOLLARS,
    MICRODOLLARS_PER_CENT,
    MICRODOLLARS_PER_DOLLAR,
    TOKENS_PER_MILLION,
    dollars_to_cents,
    dollars_to_microdollars,
    format_money_display,
    format_money_precise,
    microdollars_to_decimal,
    microdollars_to_float,
    token_cost_microdollars,
)

# Reasonable upper bound: more than enough for usage at planet-scale, well
# within int and Decimal precision. Hypothesis explores edges aggressively.
MAX_MICRODOLLARS = MAX_CHECKOUT_DOLLARS * MICRODOLLARS_PER_DOLLAR * 100


@given(microdollars=st.integers(min_value=0, max_value=MAX_MICRODOLLARS))
def test_microdollars_to_decimal_round_trips_through_dollars_to_microdollars(microdollars: int) -> None:
    """`dollars_to_microdollars(microdollars_to_decimal(x)) == x`. The
    decimal formatter must lose nothing the parser can't recover.

    Production never produces a negative microdollar value (cost = tokens
    × rate, reservations gate available > 0 before deducting), so we
    bound the property at zero. The negative-sign formatting path is
    covered by `test_negative_microdollars_carry_minus_sign` as
    defensive coverage of the symmetric formatter, not a real flow."""
    decimal = microdollars_to_decimal(microdollars)
    assert dollars_to_microdollars(decimal) == microdollars


@given(microdollars=st.integers(min_value=0, max_value=MAX_MICRODOLLARS))
def test_format_money_display_always_two_decimal_places(microdollars: int) -> None:
    """The padded formatter must always produce a `.NN` suffix so columns
    align and we never expose internal microdollar precision in the UI."""
    text = format_money_display(microdollars)
    assert text.startswith("$")
    body = text[1:]
    assert "." in body
    cents_part = body.split(".", 1)[1]
    assert len(cents_part) == 2
    assert cents_part.isdigit()


@given(microdollars=st.integers(min_value=0, max_value=MAX_MICRODOLLARS))
def test_format_money_display_rounds_to_nearest_cent(microdollars: int) -> None:
    """The padded formatter rounds half-to-even via Python's `round()`. The
    resulting cents must equal `round(microdollars / 10_000)`."""
    expected_cents = round(microdollars / MICRODOLLARS_PER_CENT)
    text = format_money_display(microdollars)
    body = text[1:]
    whole, cents = body.split(".", 1)
    actual_cents = int(whole) * 100 + int(cents)
    assert actual_cents == expected_cents


@given(microdollars=st.integers(min_value=0, max_value=MAX_MICRODOLLARS))
@example(microdollars=1)  # The smallest possible cost — must keep precision.
def test_format_money_precise_keeps_subcent_when_present(microdollars: int) -> None:
    """The precise formatter never rounds; sub-cent costs (1 microdollar
    generations) must be visible in the activity log."""
    text = format_money_precise(microdollars)
    assert text.startswith("$")
    body = text[1:]
    if microdollars == 0:
        assert body == "0.00"
        return
    if microdollars % MICRODOLLARS_PER_DOLLAR != 0:
        # Sub-dollar precision must show up.
        assert "." in body


@given(microdollars=st.integers(min_value=-MAX_MICRODOLLARS, max_value=-MICRODOLLARS_PER_CENT))
def test_negative_microdollars_carry_minus_sign(microdollars: int) -> None:
    """A refund-shaped negative value formats with `-$` not `$-`. Values
    that round to zero cents (e.g. -1 microdollar) display as `$0.00`
    without a sign — that's expected since the rounded amount is zero."""
    display = format_money_display(microdollars)
    precise = format_money_precise(microdollars)
    assert display.startswith("-$")
    assert precise.startswith("-$")
    # Never the wrong-order `$-` shape.
    assert "$-" not in display
    assert "$-" not in precise


@given(value=st.decimals(min_value=Decimal("0"), max_value=Decimal(MAX_CHECKOUT_DOLLARS), places=6))
def test_dollars_to_microdollars_is_monotonic(value: Decimal) -> None:
    """Bigger dollar amount → bigger microdollar amount. Catches sign
    flips and accidental float-truncation bugs."""
    assume(value.is_finite())
    smaller = max(Decimal("0"), value - Decimal("0.01"))
    assert dollars_to_microdollars(smaller) <= dollars_to_microdollars(value)


@given(value=st.decimals(min_value=Decimal("0"), max_value=Decimal(MAX_CHECKOUT_DOLLARS), places=2))
def test_dollars_to_cents_matches_dollars_to_microdollars_div_10000(value: Decimal) -> None:
    """Cents and microdollars must agree to the cent."""
    assume(value.is_finite())
    cents = dollars_to_cents(value)
    micros = dollars_to_microdollars(value)
    assert cents == micros // MICRODOLLARS_PER_CENT


@given(microdollars=st.integers(min_value=0, max_value=MAX_MICRODOLLARS))
def test_microdollars_to_float_loses_at_most_subcent_precision(microdollars: int) -> None:
    """The float helper is documented as compatibility-only — but it must
    not introduce more than half a microdollar of error on values inside
    our checkout range. (Float64 has ~15 digits of precision, well past
    a 10-billion-dollar cap, but we pin the bound anyway.)"""
    f = microdollars_to_float(microdollars)
    assert abs(f * MICRODOLLARS_PER_DOLLAR - microdollars) < 1


@given(
    tokens=st.integers(min_value=0, max_value=10_000_000),
    rate=st.integers(min_value=0, max_value=10 * MICRODOLLARS_PER_DOLLAR),
)
def test_token_cost_is_non_negative_and_bounded(tokens: int, rate: int) -> None:
    cost = token_cost_microdollars(tokens, rate)
    assert cost >= 0
    # Upper bound: tokens * rate / 1M, plus the round-half-up adjustment.
    upper = tokens * rate // TOKENS_PER_MILLION + 1
    assert cost <= upper


@settings(max_examples=50)
@given(microdollars=st.integers(min_value=0, max_value=MAX_MICRODOLLARS))
def test_format_money_display_is_idempotent_via_dollars_to_cents(microdollars: int) -> None:
    """Round-trip: format, re-parse the dollar string with Decimal, run
    through dollars_to_cents, must equal the rounded cents we computed."""
    expected_cents = round(microdollars / MICRODOLLARS_PER_CENT)
    text = format_money_display(microdollars)
    body = text[1:]  # strip leading "$"
    parsed = Decimal(body)
    assert dollars_to_cents(parsed) == expected_cents


def test_dollars_to_microdollars_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        dollars_to_microdollars("not-a-number")
    with pytest.raises(ValueError):
        dollars_to_microdollars(float("nan"))
    with pytest.raises(ValueError):
        dollars_to_microdollars(float("inf"))


@pytest.mark.parametrize("value", [
    "1e1000000", "-1e1000000", "1e100", "-1e100", "NaN", "sNaN",
    "Infinity", "-Infinity", "9223372036854.7758075", "-9223372036854.7758085",
])
def test_microdollar_conversion_rejects_overflow_as_value_error(value: str) -> None:
    with pytest.raises(ValueError, match="dollar amount"):
        dollars_to_microdollars(value)


@st.composite
def _lexical_dollars(draw):
    # Vary spelling independently of numeric value, including long coefficients,
    # leading signs/zeros, decimal places, and both exponent spellings.
    sign = draw(st.sampled_from(["", "+", "-"]))
    zeros = "0" * draw(st.integers(0, 12))
    whole = draw(st.text("0123456789", min_size=1, max_size=40))
    fraction = draw(st.text("0123456789", min_size=0, max_size=90))
    exponent = draw(st.one_of(st.none(), st.integers(-1000, 30)))
    value = sign + zeros + whole + ("." + fraction if fraction else "")
    if exponent is not None:
        value += draw(st.sampled_from(["e", "E"])) + f"{exponent:+d}"
    return value


def _assert_microdollar_conversion_matches_main(value: object) -> None:
    # Frozen f8c287aa5 arithmetic; bounds are independent of production constants.
    expected = int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
    if -(2**63) <= expected < 2**63:
        assert dollars_to_microdollars(value) == expected
    else:
        with pytest.raises(ValueError, match="dollar amount"):
            dollars_to_microdollars(value)


@settings(max_examples=1000, database=None)
@given(value=st.one_of(
    _lexical_dollars(),
    _lexical_dollars().map(Decimal),
    st.floats(min_value=-1e14, max_value=1e14, allow_nan=False, allow_infinity=False),
    st.integers(min_value=-(2**64), max_value=2**64),
    st.decimals(min_value="-9223372036855", max_value="9223372036855",
                places=24, allow_nan=False, allow_infinity=False),
))
@example(value="+000001E2")
@example(value="2.5e-3")
@example(value=Decimal("9223372036854.775807499999999"))
@example(value=Decimal("-9223372036854.775808499999999"))
@example(value=Decimal("0.0000005"))
@example(value=Decimal("-0.0000005"))
@example(value=Decimal("1e-1000000"))
def test_microdollar_conversion_matches_main(value: object) -> None:
    _assert_microdollar_conversion_matches_main(value)
    _assert_microdollar_conversion_matches_main(str(value))


@pytest.mark.parametrize("boundary", [
    Decimal("-9223372036854.775808"), Decimal("9223372036854.775807"),
    Decimal("1"), Decimal("20"), Decimal("9570.76"), Decimal("9992.50"),
    Decimal("9776.66"), Decimal("10000"),
])
@pytest.mark.parametrize("offset", [
    Decimal("-0.01"), Decimal("-0.000001"), Decimal("-0.0000005"), Decimal("0"),
    Decimal("0.0000005"), Decimal("0.000001"), Decimal("0.01"),
])
@pytest.mark.parametrize("representation", [str, Decimal, float, int])
def test_microdollar_conversion_matches_main_at_boundaries(boundary, offset, representation):
    value = representation(boundary + offset)
    _assert_microdollar_conversion_matches_main(value)
    _assert_microdollar_conversion_matches_main(str(value))


@pytest.mark.parametrize("value", [
    "0", "+0", "-0", "-000.000000000000000000000000000000", "+0000E+10", "-0e-1000000",
    Decimal("-0"), Decimal("0E-20"), Decimal("-0E+20"), -0.0, 0.0, 0,
])
def test_microdollar_conversion_matches_main_signed_zero(value):
    _assert_microdollar_conversion_matches_main(value)
