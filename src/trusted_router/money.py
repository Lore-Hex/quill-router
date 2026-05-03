from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MICRODOLLARS_PER_DOLLAR = 1_000_000
MICRODOLLARS_PER_CENT = 10_000
TOKENS_PER_MILLION = 1_000_000

# Default trial credit granted to a new workspace ($10).
DEFAULT_TRIAL_CREDIT_MICRODOLLARS = 10 * MICRODOLLARS_PER_DOLLAR

# Stripe checkout cap ($10,000).
MAX_CHECKOUT_DOLLARS = 10_000
MAX_CHECKOUT_MICRODOLLARS = MAX_CHECKOUT_DOLLARS * MICRODOLLARS_PER_DOLLAR


def money_pair(name: str, microdollars: int) -> dict[str, object]:
    """Return both the float-dollar field and the integer-microdollars field
    for a money value, so response shapes don't have to repeat the conversion.
    """
    return {
        name: microdollars_to_float(microdollars),
        f"{name}_microdollars": microdollars,
    }


def microdollars_to_decimal(microdollars: int) -> str:
    sign = "-" if microdollars < 0 else ""
    value = abs(int(microdollars))
    whole = value // MICRODOLLARS_PER_DOLLAR
    fraction = value % MICRODOLLARS_PER_DOLLAR
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def microdollars_per_million_tokens_to_token_decimal(microdollars: int) -> str:
    sign = "-" if microdollars < 0 else ""
    value = abs(int(microdollars))
    denominator = MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION
    whole = value // denominator
    fraction = value % denominator
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:012d}".rstrip("0")


def token_cost_microdollars(tokens: int, microdollars_per_million_tokens: int) -> int:
    raw = int(tokens) * int(microdollars_per_million_tokens)
    if raw <= 0:
        return 0
    return (raw + TOKENS_PER_MILLION // 2) // TOKENS_PER_MILLION


def microdollars_to_float(microdollars: int) -> float:
    # Compatibility only. Ledger math and dashboard rendering use integers.
    return float(microdollars) / MICRODOLLARS_PER_DOLLAR


def dollars_to_microdollars(value: object) -> int:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid dollar amount") from exc
    if not decimal.is_finite():
        raise ValueError("invalid dollar amount")
    return int((decimal * MICRODOLLARS_PER_DOLLAR).to_integral_value(rounding=ROUND_HALF_UP))


def format_money_display(microdollars: int) -> str:
    """Render microdollars as a "$1.23" / "-$0.04" display string, always
    showing two decimal places. Used by balance and limit surfaces in the
    console UI where columns benefit from aligned ".00" padding."""
    cents = int(round(microdollars / MICRODOLLARS_PER_CENT))
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def format_money_precise(microdollars: int) -> str:
    """Render microdollars preserving sub-cent precision when present.
    The activity log uses this so a 1-microdollar generation shows as
    "$0.000001" rather than rounding to "$0.00" and lying."""
    decimal = microdollars_to_decimal(microdollars)
    if decimal.startswith("-"):
        body = decimal[1:]
        return f"-${body}.00" if "." not in body else f"-${body}"
    return f"${decimal}.00" if "." not in decimal else f"${decimal}"


def dollars_to_cents(value: object) -> int:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid dollar amount") from exc
    if not decimal.is_finite():
        raise ValueError("invalid dollar amount")
    return int((decimal * 100).to_integral_value(rounding=ROUND_HALF_UP))
