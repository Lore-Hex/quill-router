from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.paypal_billing import (
    credit_paypal_capture,
    verify_paypal_webhook_signature,
)
from trusted_router.storage import STORE
from trusted_router.typed_balance import live_credit_summary


def _paypal_settings() -> Settings:
    return Settings(
        environment="test",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id=None,
        stripe_card_fee_basis_points=290,
        stripe_card_fee_fixed_cents=30,
    )


def _verified_paypal_settings() -> Settings:
    return Settings(
        environment="test",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id="paypal-webhook",
    )


def _paypal_signature_headers() -> dict[str, str]:
    return {
        "paypal-transmission-id": "transmission-1",
        "paypal-transmission-time": "2026-08-22T00:00:00Z",
        "paypal-cert-url": "https://api-m.paypal.com/cert.pem",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "signature",
    }


def _completed_order(
    *,
    workspace_id: str,
    custom_id: str,
    value: str = "26.06",
    order_id: str = "ORDER-1",
    capture_id: str = "CAPTURE-1",
) -> dict[str, Any]:
    return {
        "id": order_id,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "reference_id": workspace_id,
                "custom_id": custom_id,
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": "COMPLETED",
                            "amount": {"currency_code": "USD", "value": value},
                        }
                    ]
                },
            }
        ],
    }


def test_paypal_checkout_uses_the_existing_stripe_card_fee_schedule(
    monkeypatch: pytest.MonkeyPatch,
    user_headers: dict[str, str],
) -> None:
    app = create_app(_paypal_settings(), init_observability=False)
    captured: dict[str, Any] = {}

    def paypal_post(
        _settings: Settings,
        path: str,
        *,
        request_id: str,
        json_body: dict[str, Any],
    ) -> dict[str, Any]:
        assert path == "/v2/checkout/orders"
        assert request_id.startswith("tr-paypal-order-")
        captured.update(json_body)
        return {
            "id": "ORDER-FEE",
            "links": [{"rel": "approve", "href": "https://paypal.test/approve"}],
        }

    monkeypatch.setattr(
        "trusted_router.services.paypal_billing._paypal_post",
        paypal_post,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 25, "payment_method": "paypal"},
        )

    assert response.status_code == 201, response.text
    unit = captured["purchase_units"][0]
    assert unit["amount"] == {
        "currency_code": "USD",
        "value": "26.06",
        "breakdown": {"item_total": {"currency_code": "USD", "value": "26.06"}},
    }
    assert [(item["name"], item["unit_amount"]["value"]) for item in unit["items"]] == [
        ("TrustedRouter credits", "25.00"),
        ("Payment processing fee", "1.06"),
    ]
    reference = json.loads(unit["custom_id"])
    assert reference["c"] == 2500
    assert reference["t"] == 2606
    initiating_user = STORE.find_user_by_email("alice@example.com")
    assert initiating_user is not None
    assert reference == {
        "w": unit["reference_id"],
        "u": initiating_user.id,
        "c": 2500,
        "t": 2606,
    }
    data = response.json()["data"]
    assert data["amount_microdollars"] == 25_000_000
    assert data["processing_fee_microdollars"] == 1_060_000
    assert data["total_microdollars"] == 26_060_000


def test_paypal_capture_credits_only_principal_and_is_idempotent(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None
    order = _completed_order(
        workspace_id=workspace_id,
        custom_id=f"tr1|{workspace_id}|2500|2606",
    )

    first = credit_paypal_capture(order, expected_workspace_id=workspace_id)
    second = credit_paypal_capture(order, expected_workspace_id=workspace_id)

    assert first.credited is True
    assert second.credited is False
    assert first.amount_microdollars == 25_000_000
    assert first.processing_fee_microdollars == 1_060_000
    assert first.charge_amount_microdollars == 26_060_000
    after = live_credit_summary(workspace_id)
    assert after is not None
    assert after["total_credits"] - before["total_credits"] == 25_000_000


def test_paypal_capture_rejects_charge_that_does_not_match_reference(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None
    order = _completed_order(
        workspace_id=workspace_id,
        custom_id=f"tr1|{workspace_id}|2500|2606",
        value="26.05",
    )

    with pytest.raises(HTTPException) as raised:
        credit_paypal_capture(order, expected_workspace_id=workspace_id)

    assert raised.value.status_code == 400
    after = live_credit_summary(workspace_id)
    assert after == before


def test_legacy_paypal_capture_still_credits_the_full_capture(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = live_credit_summary(workspace_id)
    assert before is not None
    order = _completed_order(
        workspace_id=workspace_id,
        custom_id=workspace_id,
        value="3.00",
        capture_id="CAPTURE-LEGACY",
    )

    result = credit_paypal_capture(order, expected_workspace_id=workspace_id)

    assert result.amount_microdollars == 3_000_000
    assert result.processing_fee_microdollars == 0
    assert result.charge_amount_microdollars == 3_000_000
    after = live_credit_summary(workspace_id)
    assert after is not None
    assert after["total_credits"] - before["total_credits"] == 3_000_000


def test_paypal_webhook_credits_principal_and_returns_fee_breakdown(
    user_headers: dict[str, str],
) -> None:
    app = create_app(_paypal_settings(), init_observability=False)
    with TestClient(app) as client:
        workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
        event = {
            "id": "WH-1",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAPTURE-WEBHOOK",
                "status": "COMPLETED",
                "custom_id": f"tr1|{workspace_id}|2500|2606",
                "amount": {"currency_code": "USD", "value": "26.06"},
                "supplementary_data": {"related_ids": {"order_id": "ORDER-WEBHOOK"}},
            },
        }

        response = client.post("/v1/internal/paypal/webhook", json=event)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["credited"] is True
    assert data["amount_microdollars"] == 25_000_000
    assert data["processing_fee_microdollars"] == 1_060_000
    assert data["total_microdollars"] == 26_060_000


@pytest.mark.parametrize(
    ("paypal_status", "expected_status", "expected_message"),
    [
        (400, 400, "Invalid PayPal webhook"),
        (500, 502, "PayPal request failed"),
    ],
)
def test_paypal_webhook_classifies_paypal_verification_failures(
    monkeypatch: pytest.MonkeyPatch,
    paypal_status: int,
    expected_status: int,
    expected_message: str,
) -> None:
    settings = _verified_paypal_settings()

    def fake_access_token(_settings: Settings) -> str:
        return "token"

    def paypal_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            paypal_status,
            request=request,
            json={"name": "PAYPAL_TEST_FAILURE"},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "trusted_router.services.paypal_billing._access_token",
        fake_access_token,
    )
    monkeypatch.setattr(
        "trusted_router.services.paypal_billing.httpx.Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(paypal_handler)),
    )

    with pytest.raises(HTTPException) as raised:
        verify_paypal_webhook_signature(
            headers=_paypal_signature_headers(),
            event={"id": "WH-BAD"},
            settings=settings,
        )

    assert raised.value.status_code == expected_status
    assert raised.value.detail["error"]["message"] == expected_message


def test_paypal_capture_rejects_subcent_amount(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    order = _completed_order(
        workspace_id=workspace_id,
        custom_id=workspace_id,
        value="3.001",
        capture_id="CAPTURE-SUBCENT",
    )

    with pytest.raises(HTTPException) as raised:
        credit_paypal_capture(order, expected_workspace_id=workspace_id)

    assert raised.value.status_code == 400


def test_mock_paypal_checkout_reports_same_fee_breakdown(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": 25, "payment_method": "paypal"},
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["mode"] == "mock_paypal"
    assert data["amount_microdollars"] == 25_000_000
    assert data["processing_fee_microdollars"] == 1_060_000
    assert data["total_microdollars"] == 26_060_000


def test_paypal_checkout_rejects_amount_below_ten_dollars(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": "9.99", "payment_method": "paypal"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "bad_request"


def test_minimum_paypal_checkout_applies_eighty_cent_fee_floor(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": 10, "payment_method": "paypal"},
    )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["amount_microdollars"] == 10_000_000
    assert data["processing_fee_microdollars"] == 800_000
    assert data["total_microdollars"] == 10_800_000


def test_non_paypal_checkout_keeps_one_dollar_minimum(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": 1, "payment_method": "auto"},
    )

    assert response.status_code == 201, response.text


def test_paypal_capture_rejects_cross_workspace_reference(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    other = STORE.create_workspace("other-owner", "Other")
    order = _completed_order(
        workspace_id=other.id,
        custom_id=f"tr1|{other.id}|2500|2606",
        capture_id="CAPTURE-OTHER",
    )

    with pytest.raises(HTTPException) as raised:
        credit_paypal_capture(order, expected_workspace_id=workspace_id)

    assert raised.value.status_code == 403
