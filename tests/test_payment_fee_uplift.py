"""Fee uplift changes the surcharge, never the purchased credit principal."""
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.services.stripe_fees import processing_fee, scale_processing_fee_cents


@pytest.mark.parametrize(
    "principal,rate,fixed,floor,cap,expected",
    [
        (300, 290, 30, 80, None, 120),
        (2000, 290, 30, 80, None, 137),
        (2500, 290, 30, 80, None, 159),
        (2500, 150, 0, 0, None, 59),
        (2500, 80, 0, 0, 500, 32),
        (100000, 80, 0, 0, 500, 750),
        (2500, 0, 0, 0, None, 0),
    ],
)
def test_exact_new_fee_examples(principal, rate, fixed, floor, cap, expected):
    fee = processing_fee(
        credit_amount_cents=principal,
        variable_basis_points=rate,
        fixed_fee_cents=fixed,
        minimum_fee_cents=floor,
        max_fee_cents=cap,
        fee_multiplier_basis_points=15_000,
    )
    assert fee.processing_fee_cents == expected
    assert fee.charge_amount_cents == principal + expected
    assert fee.credit_amount_cents == principal
    assert fee.estimated_processor_cost_cents() <= expected
    metadata = fee.metadata(workspace_id="ws_fee_test", payment_method="card")
    assert metadata["fee_multiplier_basis_points"] == "15000"
    assert metadata["processing_fee_cents"] == str(expected)


@given(
    principal=st.integers(min_value=1, max_value=1_000_000),
    rate=st.integers(min_value=0, max_value=900),
    fixed=st.integers(min_value=0, max_value=100),
    floor=st.integers(min_value=0, max_value=200),
    cap=st.sampled_from([None, 500]),
)
def test_uplift_is_exactly_one_and_a_half_times_old_fee(principal, rate, fixed, floor, cap):
    args = dict(credit_amount_cents=principal, variable_basis_points=rate,
                fixed_fee_cents=fixed, minimum_fee_cents=floor, max_fee_cents=cap)
    before = processing_fee(**args)
    after = processing_fee(**args, fee_multiplier_basis_points=15_000)
    assert after.processing_fee_cents == (before.processing_fee_cents * 3 + 1) // 2
    assert after.credit_amount_cents == before.credit_amount_cents
    assert after.charge_amount_cents - after.estimated_processor_cost_cents() >= principal


@pytest.mark.parametrize("multiplier", [-1, 0, 9999])
def test_fee_multiplier_cannot_underfund_processor_cost(multiplier):
    with pytest.raises(ValueError, match="at least 10000"):
        processing_fee(credit_amount_cents=2500, variable_basis_points=290,
                       fixed_fee_cents=30, fee_multiplier_basis_points=multiplier)
    with pytest.raises(ValidationError, match="at least 10000"):
        Settings(stripe_paypal_fee_multiplier_basis_points=multiplier)


def test_negative_fee_cannot_be_scaled():
    with pytest.raises(ValueError, match="cannot be negative"):
        scale_processing_fee_cents(-1, 15_000)


def test_adyen_base_floor_is_unchanged():
    fee = processing_fee(credit_amount_cents=300, variable_basis_points=0,
                         fixed_fee_cents=0, minimum_fee_cents=80)
    assert fee.processing_fee_cents == 80
    assert fee.fee_multiplier_basis_points == 10_000


def test_fee_uplift_is_default_and_persisted_by_deployment():
    assert Settings().stripe_paypal_fee_multiplier_basis_points == 15_000
    script = Path("scripts/deploy/rollout.sh").read_text()
    assert '"TR_STRIPE_PAYPAL_FEE_MULTIPLIER_BASIS_POINTS=15000"' in script
