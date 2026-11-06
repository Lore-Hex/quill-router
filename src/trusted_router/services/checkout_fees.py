"""Which settings feed ``processing_fee()`` for each checkout rail.

The Stripe, PayPal and Adyen checkout services and the console Credits page
preview all price a purchase from these rates, so a rail's fee is defined in
exactly one place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from trusted_router.config import Settings
from trusted_router.money import MAX_CHECKOUT_DOLLARS
from trusted_router.services.stripe_fees import ProcessingFee, processing_fee

FeeRail = Literal["card", "ach", "stablecoin", "adyen"]
FEE_RAILS: tuple[FeeRail, ...] = ("card", "ach", "stablecoin", "adyen")


@dataclass(frozen=True)
class FeeRates:
    """The ``processing_fee()`` inputs for one rail."""

    basis_points: int
    fixed_cents: int
    minimum_cents: int
    max_cents: int | None
    multiplier_basis_points: int

    def processing_fee(self, credit_amount_cents: int) -> ProcessingFee:
        return processing_fee(
            credit_amount_cents=credit_amount_cents,
            variable_basis_points=self.basis_points,
            fixed_fee_cents=self.fixed_cents,
            minimum_fee_cents=self.minimum_cents,
            max_fee_cents=self.max_cents,
            fee_multiplier_basis_points=self.multiplier_basis_points,
        )

    def as_dict(self) -> dict[str, int | None]:
        return asdict(self)


def rail_fee_rates(settings: Settings, rail: FeeRail) -> FeeRates:
    """Card-style rails carry the purchase-fee floor and the customer
    surcharge; ACH and stablecoin keep their lower rail-specific schedule;
    Adyen prices on its own schedule with the floor and no surcharge."""
    surcharge = settings.stripe_paypal_fee_multiplier_basis_points
    if rail == "ach":
        return FeeRates(
            basis_points=settings.stripe_ach_fee_basis_points,
            fixed_cents=settings.stripe_ach_fee_fixed_cents,
            minimum_cents=0,
            max_cents=settings.stripe_ach_fee_max_cents,
            multiplier_basis_points=surcharge,
        )
    if rail == "stablecoin":
        return FeeRates(
            basis_points=settings.stripe_stablecoin_fee_basis_points,
            fixed_cents=settings.stripe_stablecoin_fee_fixed_cents,
            minimum_cents=0,
            max_cents=None,
            multiplier_basis_points=surcharge,
        )
    if rail == "adyen":
        return FeeRates(
            basis_points=settings.adyen_card_fee_basis_points,
            fixed_cents=settings.adyen_card_fee_fixed_cents,
            minimum_cents=settings.checkout_card_fee_minimum_cents,
            max_cents=None,
            multiplier_basis_points=10_000,
        )
    return FeeRates(
        basis_points=settings.stripe_card_fee_basis_points,
        fixed_cents=settings.stripe_card_fee_fixed_cents,
        minimum_cents=settings.checkout_card_fee_minimum_cents,
        max_cents=None,
        multiplier_basis_points=surcharge,
    )


def rail_processing_fee(
    settings: Settings, rail: FeeRail, credit_amount_cents: int
) -> ProcessingFee:
    return rail_fee_rates(settings, rail).processing_fee(credit_amount_cents)


def stripe_max_credit_cents(settings: Settings, rail: FeeRail) -> int:
    """Largest cent principal whose actual line-item total fits Stripe's cap.

    The fee is monotone, including its floor, cap and surcharge rounding.
    Search the same integer calculation that produces the charge rather than
    inverting an approximate percentage formula.
    """
    rates = rail_fee_rates(settings, rail)
    low, high = 0, MAX_CHECKOUT_DOLLARS * 100
    while low < high:
        middle = (low + high + 1) // 2
        if rates.processing_fee(middle).charge_amount_cents <= MAX_CHECKOUT_DOLLARS * 100:
            low = middle
        else:
            high = middle - 1
    return low
