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


def test_ach_fee_is_integer_only_and_caps_at_five_dollars() -> None:
    small = stripe_processing_fee(
        credit_amount_cents=2_500,
        variable_basis_points=80,
        fixed_fee_cents=0,
        max_fee_cents=500,
    )
    large = stripe_processing_fee(
        credit_amount_cents=100_000,
        variable_basis_points=80,
        fixed_fee_cents=0,
        max_fee_cents=500,
    )

    assert small.processing_fee_cents == 21
    assert small.charge_amount_cents == 2_521
    assert small.estimated_processor_cost_cents() == 21
    assert large.processing_fee_cents == 500
    assert large.charge_amount_cents == 100_500
    assert large.estimated_processor_cost_cents() == 500


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


def test_explicit_card_checkout_saves_only_a_reusable_card(
    monkeypatch,
    user_headers: dict[str, str],
) -> None:
    app = create_app(
        Settings(environment="test", stripe_secret_key="sk_test_card_only"),  # noqa: S106
        init_observability=False,
    )
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_card_only", "url": "https://checkout.stripe.test/card"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )

    with TestClient(app) as local_client:
        response = local_client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 20, "payment_method": "card"},
        )

    assert response.status_code == 201, response.text
    assert captured["payment_method_types"] == ["card"]
    assert captured["payment_intent_data"]["setup_future_usage"] == "off_session"


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


def test_ach_checkout_uses_bank_account_and_capped_fee_schedule(
    monkeypatch,
    user_headers: dict[str, str],
) -> None:
    app = create_app(
        Settings(environment="test", stripe_secret_key="sk_test_ach"),  # noqa: S106
        init_observability=False,
    )
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_ach", "url": "https://checkout.stripe.test/ach"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )

    with TestClient(app) as local_client:
        response = local_client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 25, "payment_method": "ach"},
        )

    assert response.status_code == 201, response.text
    assert captured["payment_method_types"] == ["us_bank_account"]
    assert captured["payment_method_options"] == {
        "us_bank_account": {"verification_method": "automatic"}
    }
    assert captured["customer_creation"] == "always"
    assert captured["payment_intent_data"] == {"metadata": captured["metadata"]}
    assert "setup_future_usage" not in captured["payment_intent_data"]
    assert [item["price_data"]["unit_amount"] for item in captured["line_items"]] == [
        2_500,
        21,
    ]
    assert captured["metadata"]["payment_method"] == "ach"
    assert captured["metadata"]["fee_max_cents"] == "500"
    assert response.json()["data"]["mode"] == "stripe_ach"
    assert response.json()["data"]["amount_microdollars"] == 25_000_000
    assert response.json()["data"]["processing_fee_microdollars"] == 210_000
    assert response.json()["data"]["total_microdollars"] == 25_210_000


def test_ach_checkout_reuses_existing_stripe_customer(
    monkeypatch,
    user_headers: dict[str, str],
) -> None:
    app = create_app(
        Settings(environment="test", stripe_secret_key="sk_test_ach"),  # noqa: S106
        init_observability=False,
    )
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_ach_existing", "url": "https://checkout.stripe.test/ach"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )

    with TestClient(app) as local_client:
        workspace_id = local_client.get(
            "/v1/workspaces", headers=user_headers
        ).json()["data"][0]["id"]
        from trusted_router.storage import STORE

        STORE.set_stripe_customer(
            workspace_id,
            customer_id="cus_existing",
            payment_method_id="pm_existing_card",
        )
        response = local_client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 1000, "payment_method": "bank"},
        )

    assert response.status_code == 201, response.text
    assert captured["customer"] == "cus_existing"
    assert "customer_creation" not in captured
    assert [item["price_data"]["unit_amount"] for item in captured["line_items"]] == [
        100_000,
        500,
    ]


def test_ach_checkout_completion_waits_for_async_success_and_credits_once(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None
    session = {
        "id": "cs_ach_delayed",
        "mode": "payment",
        "payment_intent": "pi_ach_delayed",
        "payment_status": "unpaid",
        "amount_total": 2_521,
        "customer": "cus_ach_new",
        "metadata": {
            "workspace_id": workspace_id,
            "payment_method": "ach",
            "credit_amount_microdollars": "25000000",
            "processing_fee_cents": "21",
            "charge_amount_cents": "2521",
            "fee_variable_basis_points": "80",
            "fee_fixed_cents": "0",
            "fee_max_cents": "500",
        },
    }

    pending = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_ach_completed",
            "type": "checkout.session.completed",
            "data": {"object": session},
        },
    )
    after_pending = live_credit_summary(workspace_id)
    assert pending.status_code == 200, pending.text
    assert pending.json()["data"]["payment_pending"] is True
    assert pending.json()["data"]["credited"] is False
    assert after_pending == before

    session["payment_status"] = "paid"
    first = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_ach_async_succeeded_1",
            "type": "checkout.session.async_payment_succeeded",
            "data": {"object": session},
        },
    )
    replay_with_distinct_event = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_ach_async_succeeded_2",
            "type": "checkout.session.async_payment_succeeded",
            "data": {"object": session},
        },
    )
    completed_after_success = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_ach_completed_paid",
            "type": "checkout.session.completed",
            "data": {"object": session},
        },
    )

    assert first.status_code == 200, first.text
    assert first.json()["data"]["credited"] is True
    assert replay_with_distinct_event.json()["data"]["credited"] is False
    assert completed_after_success.json()["data"]["credited"] is False
    after = live_credit_summary(workspace_id)
    assert after is not None
    assert after["total_credits"] - before["total_credits"] == 25_000_000


def test_ach_async_failure_never_credits_or_replaces_saved_card(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    from trusted_router.storage import STORE

    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    STORE.set_stripe_customer(
        workspace_id,
        customer_id="cus_saved_card",
        payment_method_id="pm_saved_card",
    )
    before = live_credit_summary(workspace_id)
    assert before is not None

    failed = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_ach_async_failed",
            "type": "checkout.session.async_payment_failed",
            "data": {
                "object": {
                    "id": "cs_ach_failed",
                    "mode": "payment",
                    "payment_intent": "pi_ach_failed",
                    "payment_status": "unpaid",
                    "amount_total": 2_521,
                    "customer": "cus_ach_other",
                    "metadata": {
                        "workspace_id": workspace_id,
                        "payment_method": "ach",
                    },
                }
            },
        },
    )
    payment_intent = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_ach_payment_intent_succeeded",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_ach_failed",
                    "customer": "cus_ach_other",
                    "payment_method": "pm_ach_other",
                    "metadata": {
                        "workspace_id": workspace_id,
                        "payment_method": "ach",
                    },
                }
            },
        },
    )

    assert failed.status_code == 200, failed.text
    assert failed.json()["data"]["payment_failed"] is True
    assert failed.json()["data"]["credited"] is False
    assert payment_intent.json()["data"]["ignored"] is True
    after = live_credit_summary(workspace_id)
    assert after == before
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.stripe_customer_id == "cus_saved_card"
    assert account.stripe_payment_method_id == "pm_saved_card"


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
                "payment_status": "paid",
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
                    "payment_status": "paid",
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
