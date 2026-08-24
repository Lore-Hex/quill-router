"""Fixed-price billing contracts for partner-branded orchestration routes."""

from __future__ import annotations

from enum import StrEnum

from trusted_router.catalog_data import PARASAIL_LIBERTY_2_0_MODEL_ID
from trusted_router.money import token_cost_microdollars

PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE = "partner.parasail.liberty-2.0.top_level"
PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX = "partner.parasail.liberty-2.0.internal."
PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX = "partner:parasail-liberty-2.0:"
PARTNER_OPERATOR_COST_SETTLE_FIELD = "_trustedrouter_operator_cost_microdollars"

PARASAIL_LIBERTY_2_0_INPUT_MICRODOLLARS_PER_MILLION = 2_000_000
PARASAIL_LIBERTY_2_0_OUTPUT_MICRODOLLARS_PER_MILLION = 19_000_000
PARASAIL_LIBERTY_2_0_MINIMUM_CHARGE_MICRODOLLARS = 1_000


class PartnerBillingMode(StrEnum):
    TOP_LEVEL = "top_level"
    INTERNAL = "internal"


def partner_billing_mode(
    *,
    requested_model_id: str | None,
    route_type: str | None,
    idempotency_key: str | None,
) -> PartnerBillingMode | None:
    """Classify a partner charge, rejecting partial/mismatched markers.

    Internal calls are free to the customer only when both enclave-owned
    markers are present. The public alias is fixed-price only on its explicit
    top-level route. This fails closed if a future code path forgets either
    marker instead of silently under- or over-charging.
    """

    requested_model = (requested_model_id or "").strip().lower()
    route = (route_type or "").strip().lower()
    idempotency = (idempotency_key or "").strip()

    is_partner_model = requested_model == PARASAIL_LIBERTY_2_0_MODEL_ID
    is_top_route = route == PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE
    is_internal_route = route.startswith(PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX)
    is_internal_idempotency = idempotency.startswith(
        PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX
    )

    if is_partner_model or is_top_route:
        if not (is_partner_model and is_top_route):
            raise ValueError("Parasail Liberty top-level billing markers do not match")
        if is_internal_idempotency:
            raise ValueError("Parasail Liberty top-level request used an internal idempotency key")
        return PartnerBillingMode.TOP_LEVEL

    if is_internal_route or is_internal_idempotency:
        if not (is_internal_route and is_internal_idempotency):
            raise ValueError("Parasail Liberty internal billing markers do not match")
        return PartnerBillingMode.INTERNAL

    return None


def partner_cost_microdollars(
    mode: PartnerBillingMode,
    *,
    input_tokens: int,
    output_tokens: int,
) -> int:
    if mode == PartnerBillingMode.INTERNAL:
        return 0
    token_cost = token_cost_microdollars(
        input_tokens,
        PARASAIL_LIBERTY_2_0_INPUT_MICRODOLLARS_PER_MILLION,
    ) + token_cost_microdollars(
        output_tokens,
        PARASAIL_LIBERTY_2_0_OUTPUT_MICRODOLLARS_PER_MILLION,
    )
    return max(token_cost, PARASAIL_LIBERTY_2_0_MINIMUM_CHARGE_MICRODOLLARS)
