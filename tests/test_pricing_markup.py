from __future__ import annotations

from decimal import Decimal

from trusted_router.pricing import (
    _PRICE_FLOOR_MICRODOLLARS_PER_M,
    _PRICE_MARKUP_RATIO,
    _customer_price,
)


def test_prepaid_customer_price_uses_exact_five_percent_markup() -> None:
    assert _PRICE_MARKUP_RATIO == Decimal("1.05")
    assert _customer_price(1_000_000) == 1_050_000
    assert _customer_price(1_250_000) == 1_312_500


def test_prepaid_customer_price_keeps_integer_floor() -> None:
    assert _customer_price(0) == _PRICE_FLOOR_MICRODOLLARS_PER_M
    assert _customer_price(1) == _PRICE_FLOOR_MICRODOLLARS_PER_M
    assert isinstance(_customer_price(123_457), int)
