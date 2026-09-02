from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trusted_router.custom_model_markup_billing import (
    MAX_CUSTOM_MODEL_MARKUP_BASIS_POINTS,
    collected_custom_model_markup_microdollars,
    custom_model_markup_authorization_id_from_payout_event_id,
    custom_model_markup_microdollars,
    custom_model_markup_microdollars_from_charge,
    custom_model_markup_owner_share_microdollars,
    custom_model_markup_payout_event_id,
    validate_custom_model_markup_basis_points,
)


@given(
    token_cost=st.integers(min_value=0, max_value=10**15),
    basis_points=st.integers(
        min_value=0,
        max_value=MAX_CUSTOM_MODEL_MARKUP_BASIS_POINTS,
    ),
)
def test_custom_model_markup_integer_money_conserves_creator_and_router_shares(
    token_cost: int,
    basis_points: int,
) -> None:
    markup = custom_model_markup_microdollars(token_cost, basis_points)
    creator = custom_model_markup_owner_share_microdollars(markup)
    router = markup - creator
    assert markup == token_cost * basis_points // 10_000
    assert 0 <= creator <= markup
    assert creator + router == markup


@given(
    token_cost=st.integers(min_value=0, max_value=10**15),
    basis_points=st.integers(
        min_value=0,
        max_value=MAX_CUSTOM_MODEL_MARKUP_BASIS_POINTS,
    ),
)
def test_custom_model_markup_recovered_from_collected_charge_is_conservative(
    token_cost: int,
    basis_points: int,
) -> None:
    proposed_markup = custom_model_markup_microdollars(token_cost, basis_points)
    collected_charge = token_cost + proposed_markup
    recovered_markup = custom_model_markup_microdollars_from_charge(
        collected_charge,
        basis_points,
    )
    assert 0 <= recovered_markup <= proposed_markup
    assert recovered_markup <= collected_charge


@given(
    final_charge=st.integers(min_value=0, max_value=10**15),
    custom_basis_points=st.integers(
        min_value=0,
        max_value=MAX_CUSTOM_MODEL_MARKUP_BASIS_POINTS,
    ),
    app_basis_points=st.integers(min_value=0, max_value=30_000),
    additional_cost=st.integers(min_value=0, max_value=10**12),
)
def test_collected_custom_markup_never_exceeds_the_final_charge(
    final_charge: int,
    custom_basis_points: int,
    app_basis_points: int,
    additional_cost: int,
) -> None:
    markup = collected_custom_model_markup_microdollars(
        final_charge,
        custom_basis_points,
        app_markup_basis_points=app_basis_points,
        additional_cost_microdollars=additional_cost,
    )

    assert 0 <= markup <= final_charge


@pytest.mark.parametrize("value", [-1, 30_001, True, 1.5, "100"])
def test_custom_model_markup_rejects_non_integer_or_out_of_range_values(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="custom_model_markup_out_of_bounds"):
        validate_custom_model_markup_basis_points(value)  # type: ignore[arg-type]


def test_custom_model_markup_payout_event_is_authorization_scoped() -> None:
    event_id = custom_model_markup_payout_event_id("gwa_123")
    assert event_id == "custom_model_markup_payout:gwa_123"
    assert custom_model_markup_authorization_id_from_payout_event_id(event_id) == "gwa_123"
    assert custom_model_markup_authorization_id_from_payout_event_id("other:gwa_123") is None
    assert custom_model_markup_authorization_id_from_payout_event_id(
        "custom_model_markup_payout:"
    ) is None
