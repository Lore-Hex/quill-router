from __future__ import annotations

from decimal import Decimal

import pytest

from trusted_router.pricing import (
    _PRICE_FLOOR_MICRODOLLARS_PER_M,
    _PRICE_MARKUP_RATIO,
    SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS,
    _customer_price,
    customer_fixed_price_microdollars,
    signed_receipt_price_microdollars,
)


def test_prepaid_customer_price_uses_exact_five_point_five_percent_markup() -> None:
    assert _PRICE_MARKUP_RATIO == Decimal("1.055")
    assert _customer_price(1_000_000) == 1_055_000
    assert _customer_price(1_250_000) == 1_318_750


def test_prepaid_customer_price_keeps_integer_floor() -> None:
    assert _customer_price(0) == _PRICE_FLOOR_MICRODOLLARS_PER_M
    assert _customer_price(1) == _PRICE_FLOOR_MICRODOLLARS_PER_M
    assert _customer_price(123_457) == 130_248
    assert isinstance(_customer_price(123_457), int)


def test_fixed_provider_charge_uses_markup_without_token_floor() -> None:
    assert customer_fixed_price_microdollars(0) == 0
    assert customer_fixed_price_microdollars(5_000) == 5_275
    assert customer_fixed_price_microdollars(30_000) == 31_650


@pytest.mark.parametrize("value", [-1, True, 1.5, "5000"])
def test_fixed_provider_charge_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        customer_fixed_price_microdollars(value)  # type: ignore[arg-type]


def test_signed_receipt_price_is_twelve_percent_total_not_additive() -> None:
    assert SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS == 1_200
    assert signed_receipt_price_microdollars(_customer_price(1_000_000)) == 1_120_000
    assert signed_receipt_price_microdollars(0) == 0


@pytest.mark.parametrize("value", [-1, 1.5, True, "100"])
def test_signed_receipt_price_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        signed_receipt_price_microdollars(value)  # type: ignore[arg-type]
