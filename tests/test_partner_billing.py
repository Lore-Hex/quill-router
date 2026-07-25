from __future__ import annotations

import pytest

from trusted_router.partner_billing import (
    PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX,
    PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX,
    PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
    PartnerBillingMode,
    partner_billing_mode,
    partner_cost_microdollars,
)


def test_partner_top_level_cost_uses_exact_integer_token_rates() -> None:
    assert (
        partner_cost_microdollars(
            PartnerBillingMode.TOP_LEVEL,
            input_tokens=1_234_567,
            output_tokens=89_012,
        )
        == 4_249_374
    )


def test_partner_internal_cost_is_zero_even_for_large_fanout_calls() -> None:
    assert (
        partner_cost_microdollars(
            PartnerBillingMode.INTERNAL,
            input_tokens=10_000_000,
            output_tokens=10_000_000,
        )
        == 0
    )


def test_partner_billing_classifies_matching_top_and_internal_markers() -> None:
    assert (
        partner_billing_mode(
            requested_model_id="parasail/liberty-2.0",
            route_type=PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
            idempotency_key="req-top",
        )
        == PartnerBillingMode.TOP_LEVEL
    )
    assert (
        partner_billing_mode(
            requested_model_id="nvidia/nemotron-3-ultra-550b-a55b",
            route_type=f"{PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX}advisor.worker",
            idempotency_key=f"{PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX}req:worker",
        )
        == PartnerBillingMode.INTERNAL
    )


@pytest.mark.parametrize(
    ("requested_model_id", "route_type", "idempotency_key"),
    [
        ("parasail/liberty-2.0", "chat.completions", "req-top"),
        (
            "nvidia/nemotron-3-ultra-550b-a55b",
            f"{PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX}advisor.worker",
            "req-without-prefix",
        ),
        (
            "nvidia/nemotron-3-ultra-550b-a55b",
            "advisor.worker",
            f"{PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX}req-without-route",
        ),
    ],
)
def test_partner_billing_rejects_partial_markers(
    requested_model_id: str,
    route_type: str,
    idempotency_key: str,
) -> None:
    with pytest.raises(ValueError, match="markers do not match"):
        partner_billing_mode(
            requested_model_id=requested_model_id,
            route_type=route_type,
            idempotency_key=idempotency_key,
        )
