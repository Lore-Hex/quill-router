from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.stripe_fees import stripe_processing_fee
from trusted_router.typed_balance import live_credit_summary


def test_card_fee_grosses_up_principal_without_float_math() -> None:
    fee = stripe_processing_fee(
        credit_amount_cents=2_500,
        variable_basis_points=290,
        fixed_fee_cents=30,
    )

    assert fee.credit_amount_cents == 2_500
    assert fee.processing_fee_cents == 106
    assert fee.charge_amount_cents == 2_606
    assert fee.estimated_processor_cost_cents() <= fee.processing_fee_cents


def test_stablecoin_fee_grosses_up_principal_without_fixed_fee() -> None:
    fee = stripe_processing_fee(
        credit_amount_cents=2_500,
        variable_basis_points=150,
        fixed_fee_cents=0,
    )

    assert fee.processing_fee_cents == 39
    assert fee.charge_amount_cents == 2_539
    assert fee.estimated_processor_cost_cents() <= fee.processing_fee_cents


@given(
    credit_amount_cents=st.integers(min_value=100, max_value=1_000_000),
    variable_basis_points=st.integers(min_value=0, max_value=900),
    fixed_fee_cents=st.integers(min_value=0, max_value=100),
)
def test_processing_fee_is_minimal_and_always_preserves_principal(
    credit_amount_cents: int,
    variable_basis_points: int,
    fixed_fee_cents: int,
) -> None:
    fee = stripe_processing_fee(
        credit_amount_cents=credit_amount_cents,
        variable_basis_points=variable_basis_points,
        fixed_fee_cents=fixed_fee_cents,
    )

    net_cents = fee.charge_amount_cents - fee.estimated_processor_cost_cents()
    assert net_cents >= credit_amount_cents
    if fee.charge_amount_cents > credit_amount_cents:
        lower_charge = fee.charge_amount_cents - 1
        lower_cost = (
            lower_charge * variable_basis_points + 9_999
        ) // 10_000 + fixed_fee_cents
        assert lower_charge - lower_cost < credit_amount_cents


def test_checkout_rejects_subcent_credit_amount() -> None:
    with pytest.raises(ValidationError, match="exactly representable in cents"):
        CheckoutRequest(amount="25.000001")


def test_card_checkout_uses_separate_credit_and_processing_fee_line_items(
    monkeypatch,
    user_headers: dict[str, str],
) -> None:
    app = create_app(
        Settings(environment="test", stripe_secret_key="sk_test_checkout"),  # noqa: S106
        init_observability=False,
    )
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_fee", "url": "https://checkout.stripe.test/fee"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )

    with TestClient(app) as local_client:
        response = local_client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 25, "payment_method": "auto"},
        )

    assert response.status_code == 201, response.text
    line_items = captured["line_items"]
    assert [item["price_data"]["product_data"]["name"] for item in line_items] == [
        "TrustedRouter credits",
        "Payment processing fee",
    ]
    assert [item["price_data"]["unit_amount"] for item in line_items] == [2_500, 106]
    assert captured["metadata"]["credit_amount_microdollars"] == "25000000"
    assert captured["metadata"]["processing_fee_cents"] == "106"
    assert captured["metadata"]["charge_amount_cents"] == "2606"
    assert captured["payment_intent_data"]["metadata"] == captured["metadata"]

    data = response.json()["data"]
    assert data["amount_microdollars"] == 25_000_000
    assert data["processing_fee_microdollars"] == 1_060_000
    assert data["total_microdollars"] == 26_060_000


def test_stablecoin_checkout_uses_stablecoin_fee_schedule(
    monkeypatch,
    user_headers: dict[str, str],
) -> None:
    app = create_app(
        Settings(environment="test", stripe_secret_key="sk_test_stablecoin"),  # noqa: S106
        init_observability=False,
    )
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_stablecoin_fee", "url": "https://checkout.stripe.test/stablecoin"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )

    with TestClient(app) as local_client:
        response = local_client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 25, "payment_method": "stablecoin"},
        )

    assert response.status_code == 201, response.text
    assert captured["payment_method_types"] == ["crypto"]
    assert [item["price_data"]["unit_amount"] for item in captured["line_items"]] == [
        2_500,
        39,
    ]
    assert captured["metadata"]["processing_fee_cents"] == "39"
    assert captured["payment_intent_data"]["metadata"] == captured["metadata"]
    assert response.json()["data"]["total_microdollars"] == 25_390_000


def test_checkout_webhook_credits_only_requested_principal_when_total_includes_fee(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None

    event = {
        "id": "evt_checkout_with_processing_fee",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "payment",
                "amount_total": 2_606,
                "customer": "cus_fee",
                "metadata": {
                    "workspace_id": workspace_id,
                    "credit_amount_microdollars": "25000000",
                    "processing_fee_cents": "106",
                    "charge_amount_cents": "2606",
                },
            }
        },
    }

    first = client.post("/v1/internal/stripe/webhook", json=event)
    second = client.post("/v1/internal/stripe/webhook", json=event)

    assert first.status_code == 200, first.text
    assert first.json()["data"]["credited"] is True
    assert first.json()["data"]["credited_microdollars"] == 25_000_000
    assert second.json()["data"]["credited"] is False
    after = live_credit_summary(workspace_id)
    assert after is not None
    assert after["total_credits"] - before["total_credits"] == 25_000_000


def test_checkout_webhook_rejects_principal_larger_than_total_charge(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None

    response = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_checkout_invalid_principal",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "payment",
                    "amount_total": 1_000,
                    "metadata": {
                        "workspace_id": workspace_id,
                        "credit_amount_microdollars": "11000000",
                    },
                }
            },
        },
    )

    assert response.status_code == 400
    after = live_credit_summary(workspace_id)
    assert after is not None
    assert after["total_credits"] == before["total_credits"]


def test_auto_refill_webhook_validates_charge_but_credits_only_principal(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None

    response = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_refill_with_processing_fee",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_refill_with_processing_fee",
                    "amount": 2_091,
                    "metadata": {
                        "workspace_id": workspace_id,
                        "payment_method": "card",
                        "auto_refill": "true",
                        "amount_microdollars": "20000000",
                        "credit_amount_microdollars": "20000000",
                        "processing_fee_cents": "91",
                        "charge_amount_cents": "2091",
                    },
                }
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["credited"] is True
    after = live_credit_summary(workspace_id)
    assert after is not None
    assert after["total_credits"] - before["total_credits"] == 20_000_000


def test_auto_refill_webhook_rejects_fee_metadata_mismatch(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None

    response = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_refill_bad_processing_fee",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_refill_bad_processing_fee",
                    "amount": 2_091,
                    "metadata": {
                        "workspace_id": workspace_id,
                        "payment_method": "card",
                        "auto_refill": "true",
                        "amount_microdollars": "20000000",
                        "credit_amount_microdollars": "20000000",
                        "processing_fee_cents": "90",
                        "charge_amount_cents": "2091",
                    },
                }
            },
        },
    )

    assert response.status_code == 400
    after = live_credit_summary(workspace_id)
    assert after is not None
    assert after["total_credits"] == before["total_credits"]
