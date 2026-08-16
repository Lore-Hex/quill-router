from __future__ import annotations

import pytest

from trusted_router.custom_model_billing import (
    CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS,
    HUMAN_PRICE_MAX_MICRODOLLARS_PER_M,
    HUMAN_PRICE_MIN_MICRODOLLARS_PER_M,
    MACHINE_PRICE_MAX_MICRODOLLARS_PER_M,
    custom_model_cost_microdollars,
    owner_share_microdollars,
    validate_custom_model_price,
)
from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    TOKENS_PER_MILLION,
    VERIFF_ATTEMPT_FEE_MICRODOLLARS,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
)


@pytest.mark.parametrize("kind", ["human"])
def test_human_price_bounds_include_both_edges(kind: str) -> None:
    validate_custom_model_price(
        HUMAN_PRICE_MIN_MICRODOLLARS_PER_M,
        HUMAN_PRICE_MAX_MICRODOLLARS_PER_M,
        kind=kind,
    )
    for prompt, completion in (
        (HUMAN_PRICE_MIN_MICRODOLLARS_PER_M - 1, HUMAN_PRICE_MIN_MICRODOLLARS_PER_M),
        (HUMAN_PRICE_MIN_MICRODOLLARS_PER_M, HUMAN_PRICE_MIN_MICRODOLLARS_PER_M - 1),
        (HUMAN_PRICE_MAX_MICRODOLLARS_PER_M + 1, HUMAN_PRICE_MAX_MICRODOLLARS_PER_M),
        (HUMAN_PRICE_MIN_MICRODOLLARS_PER_M, HUMAN_PRICE_MAX_MICRODOLLARS_PER_M + 1),
    ):
        with pytest.raises(ValueError, match="^custom_model_price_out_of_bounds$"):
            validate_custom_model_price(prompt, completion, kind=kind)


@pytest.mark.parametrize("kind", ["machine", "agent"])
def test_machine_and_agent_price_bounds_include_both_edges(kind: str) -> None:
    validate_custom_model_price(0, MACHINE_PRICE_MAX_MICRODOLLARS_PER_M, kind=kind)
    for prompt, completion in (
        (-1, 0),
        (0, -1),
        (MACHINE_PRICE_MAX_MICRODOLLARS_PER_M + 1, MACHINE_PRICE_MAX_MICRODOLLARS_PER_M),
        (0, MACHINE_PRICE_MAX_MICRODOLLARS_PER_M + 1),
    ):
        with pytest.raises(ValueError, match="^custom_model_price_out_of_bounds$"):
            validate_custom_model_price(prompt, completion, kind=kind)


def test_unknown_custom_model_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="^custom_model_price_out_of_bounds$"):
        validate_custom_model_price(0, 0, kind="other")


def test_price_constants_have_the_documented_dollar_per_token_values() -> None:
    denominator = MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION
    assert HUMAN_PRICE_MIN_MICRODOLLARS_PER_M / denominator == pytest.approx(0.10)
    assert HUMAN_PRICE_MAX_MICRODOLLARS_PER_M / denominator == pytest.approx(1.00)
    assert MACHINE_PRICE_MAX_MICRODOLLARS_PER_M / MICRODOLLARS_PER_DOLLAR == 1_000
    assert CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS == 7_000
    assert VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS == 25 * MICRODOLLARS_PER_DOLLAR
    assert VERIFF_ATTEMPT_FEE_MICRODOLLARS == 5 * MICRODOLLARS_PER_DOLLAR


def test_custom_model_cost_adds_prompt_and_completion_sides() -> None:
    assert (
        custom_model_cost_microdollars(
            input_tokens=2_000,
            output_tokens=3_000,
            prompt_price=2_000_000,
            completion_price=4_000_000,
        )
        == 16_000
    )


def test_positive_but_sub_microdollar_charge_floors_at_one() -> None:
    assert (
        custom_model_cost_microdollars(
            input_tokens=1,
            output_tokens=0,
            prompt_price=1,
            completion_price=0,
        )
        == 1
    )


def test_zero_price_model_costs_zero() -> None:
    assert (
        custom_model_cost_microdollars(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            prompt_price=0,
            completion_price=0,
        )
        == 0
    )


@pytest.mark.parametrize("charge", [0, 1, 2, 3, 9, 10, 11, 99, 101, 1_000_003])
def test_owner_and_tr_shares_conserve_every_microdollar(charge: int) -> None:
    owner = owner_share_microdollars(charge)
    trusted_router = charge - owner
    assert owner + trusted_router == charge
    assert trusted_router == (charge * 3_000 + 9_999) // 10_000
