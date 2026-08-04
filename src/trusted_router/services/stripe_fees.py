"""Integer-only payment processing-fee calculation and line-item shaping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.money import MICRODOLLARS_PER_CENT

CREDITS_LINE_ITEM_NAME = "TrustedRouter credits"
PROCESSING_FEE_LINE_ITEM_NAME = "Payment processing fee"


@dataclass(frozen=True)
class ProcessingFee:
    """A charge whose net principal remains the requested credit amount."""

    credit_amount_cents: int
    processing_fee_cents: int
    charge_amount_cents: int
    variable_basis_points: int
    fixed_fee_cents: int
    max_fee_cents: int | None = None

    @property
    def credit_amount_microdollars(self) -> int:
        return self.credit_amount_cents * MICRODOLLARS_PER_CENT

    @property
    def processing_fee_microdollars(self) -> int:
        return self.processing_fee_cents * MICRODOLLARS_PER_CENT

    @property
    def charge_amount_microdollars(self) -> int:
        return self.charge_amount_cents * MICRODOLLARS_PER_CENT

    def estimated_processor_cost_cents(self) -> int:
        variable = _ceil_div(
            self.charge_amount_cents * self.variable_basis_points,
            10_000,
        )
        cost = variable + self.fixed_fee_cents
        if self.max_fee_cents is not None:
            return min(cost, self.max_fee_cents)
        return cost

    def checkout_line_items(self) -> list[dict[str, Any]]:
        items = [
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": CREDITS_LINE_ITEM_NAME},
                    "unit_amount": self.credit_amount_cents,
                },
                "quantity": 1,
            }
        ]
        if self.processing_fee_cents:
            items.append(
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": PROCESSING_FEE_LINE_ITEM_NAME},
                        "unit_amount": self.processing_fee_cents,
                    },
                    "quantity": 1,
                }
            )
        return items

    def payment_intent_line_items(self) -> list[dict[str, Any]]:
        items = [
            {
                "product_name": CREDITS_LINE_ITEM_NAME,
                "quantity": 1,
                "unit_cost": self.credit_amount_cents,
            }
        ]
        if self.processing_fee_cents:
            items.append(
                {
                    "product_name": PROCESSING_FEE_LINE_ITEM_NAME,
                    "quantity": 1,
                    "unit_cost": self.processing_fee_cents,
                }
            )
        return items

    def metadata(
        self,
        *,
        workspace_id: str,
        payment_method: str,
    ) -> dict[str, str]:
        metadata = {
            "workspace_id": workspace_id,
            "payment_method": payment_method,
            "credit_amount_microdollars": str(self.credit_amount_microdollars),
            "processing_fee_cents": str(self.processing_fee_cents),
            "charge_amount_cents": str(self.charge_amount_cents),
            "fee_variable_basis_points": str(self.variable_basis_points),
            "fee_fixed_cents": str(self.fixed_fee_cents),
        }
        if self.max_fee_cents is not None:
            metadata["fee_max_cents"] = str(self.max_fee_cents)
        return metadata


def processing_fee(
    *,
    credit_amount_cents: int,
    variable_basis_points: int,
    fixed_fee_cents: int,
    max_fee_cents: int | None = None,
) -> ProcessingFee:
    """Gross up a USD-cent principal so it survives the configured fee.

    If the processor charges ``rate * total + fixed``, the total required
    to preserve ``principal`` is ``ceil((principal + fixed) / (1-rate))``.
    All operations stay integer-only and round in the customer's favor only
    when doing so still covers the configured processing cost.
    """
    if credit_amount_cents <= 0:
        raise ValueError("credit_amount_cents must be positive")
    if not 0 <= variable_basis_points < 10_000:
        raise ValueError("variable_basis_points must be between 0 and 9999")
    if fixed_fee_cents < 0:
        raise ValueError("fixed_fee_cents cannot be negative")
    if max_fee_cents is not None and max_fee_cents < 0:
        raise ValueError("max_fee_cents cannot be negative")

    denominator = 10_000 - variable_basis_points
    uncapped_charge_amount_cents = _ceil_div(
        (credit_amount_cents + fixed_fee_cents) * 10_000,
        denominator,
    )
    if (
        max_fee_cents is not None
        and uncapped_charge_amount_cents - credit_amount_cents > max_fee_cents
    ):
        charge_amount_cents = credit_amount_cents + max_fee_cents
    else:
        charge_amount_cents = uncapped_charge_amount_cents
    processing_fee_cents = charge_amount_cents - credit_amount_cents
    return ProcessingFee(
        credit_amount_cents=credit_amount_cents,
        processing_fee_cents=processing_fee_cents,
        charge_amount_cents=charge_amount_cents,
        variable_basis_points=variable_basis_points,
        fixed_fee_cents=fixed_fee_cents,
        max_fee_cents=max_fee_cents,
    )


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


# Compatibility names for the existing Stripe billing adapter and SDK tests.
StripeProcessingFee = ProcessingFee
stripe_processing_fee = processing_fee
