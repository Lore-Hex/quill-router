from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE


def _x402_client(**settings: Any) -> TestClient:
    merged: dict[str, Any] = {
        "environment": "test",
        "x402_enabled": True,
        "stripe_secret_key": "sk_test_x402",
        "stripe_webhook_secret": None,
        "rate_limit_enabled": False,
    }
    merged.update(settings)
    return TestClient(create_app(Settings(**merged), init_observability=False))


def _api_headers(client: TestClient) -> tuple[dict[str, str], str]:
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "x402@example.com"},
        json={"name": "x402 key"},
    )
    assert created.status_code == 201, created.text
    raw_key = created.json()["key"]
    workspace_id = created.json()["data"]["workspace_id"]
    return {"authorization": f"Bearer {raw_key}"}, workspace_id


def _payment_intent(
    *,
    payment_intent_id: str = "pi_x402_good",
    workspace_id: str,
    status: str = "succeeded",
    currency: str = "usd",
    amount_cents: int = 1000,
    received_cents: int | None = 1000,
    requested_microdollars: int = 10_000_000,
    asset_code: str = "usdc",
    network: str = "base",
    payment_method: str = "x402",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": payment_intent_id,
        "status": status,
        "currency": currency,
        "amount": amount_cents,
        "metadata": {
            "workspace_id": workspace_id,
            "amount_microdollars": str(requested_microdollars),
            "payment_method": payment_method,
            "purpose": "trustedrouter_credits",
            "asset": asset_code.upper(),
            "network": network,
        },
        "next_action": {
            "crypto_display_details": {
                "deposit_addresses": {
                    network: {
                        "address": "0x0000000000000000000000000000000000000402",
                        "supported_tokens": [{"token_currency": asset_code}],
                    }
                }
            }
        },
    }
    if received_cents is not None:
        body["amount_received"] = received_cents
    return body


def test_x402_disabled_returns_404_without_auth(client: TestClient) -> None:
    response = client.post("/v1/billing/x402/fund", json={"amount": "10.00"})

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


def test_x402_config_refuses_mock_or_missing_stripe_outside_local_test() -> None:
    with pytest.raises(ValueError, match="TR_X402_ALLOW_MOCK_PAYMENTS"):
        Settings(environment="staging", x402_allow_mock_payments=True)
    with pytest.raises(ValueError, match="TR_X402_ENABLED"):
        Settings(
            environment="staging",
            x402_enabled=True,
            stripe_secret_key=None,
            stripe_webhook_secret=None,
        )


def test_x402_enabled_without_stripe_secret_does_not_mock_credit() -> None:
    with _x402_client(stripe_secret_key=None) as client:
        headers, workspace_id = _api_headers(client)
        before = STORE.get_credit_account(workspace_id)
        assert before is not None
        before_total = before.total_credits_microdollars
        fund = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "10.00"},
        )
        settle = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": f"pi_x402_mock_{workspace_id}_attacker"},
        )
        after = STORE.get_credit_account(workspace_id)

    assert fund.status_code == 503
    assert settle.status_code == 503
    assert after is not None
    assert after.total_credits_microdollars == before_total


def test_x402_fund_creates_stripe_crypto_payment_intent_and_returns_payment_required(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def create_payment_intent(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _payment_intent(
            payment_intent_id="pi_x402_challenge",
            workspace_id=str(kwargs["metadata"]["workspace_id"]),
            status="requires_action",
            received_cents=None,
        )

    monkeypatch.setattr(
        "trusted_router.services.x402_billing.stripe.PaymentIntent.create",
        create_payment_intent,
    )
    with _x402_client() as client:
        headers, workspace_id = _api_headers(client)
        response = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "10.00", "prompt": "secret prompt must not appear"},
        )

    assert response.status_code == 400
    assert "Extra inputs are not permitted" in response.text

    with _x402_client() as client:
        headers, workspace_id = _api_headers(client)
        response = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "10.00"},
        )

    assert response.status_code == 402, response.text
    assert "payment-required" in response.headers
    body = response.json()
    assert body["data"]["payment_intent_id"] == "pi_x402_challenge"
    assert body["data"]["amount_decimal"] == "10"
    decoded = json.loads(base64.b64decode(response.headers["payment-required"]))
    assert decoded["metadata"]["payment_intent_id"] == "pi_x402_challenge"
    assert decoded["accepts"][0]["network"] == "eip155:8453"
    assert captured["payment_method_types"] == ["crypto"]
    assert captured["payment_method_data"] == {"type": "crypto"}
    assert captured["payment_method_options"]["crypto"]["mode"] == "deposit"
    assert captured["payment_method_options"]["crypto"]["deposit_options"]["networks"] == ["base"]
    assert captured["metadata"] == {
        "workspace_id": workspace_id,
        "amount_microdollars": "10000000",
        "payment_method": "x402",
        "purpose": "trustedrouter_credits",
        "asset": "USDC",
        "network": "base",
    }
    assert captured["stripe_version"] == "2026-03-04.preview"
    assert "secret prompt" not in json.dumps(captured)
    assert "sk-tr-" not in json.dumps(captured)


def test_x402_fund_rejects_workspace_id_and_cent_fraction_before_stripe(monkeypatch) -> None:
    called = False

    def create_payment_intent(**_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        "trusted_router.services.x402_billing.stripe.PaymentIntent.create",
        create_payment_intent,
    )
    with _x402_client() as client:
        headers, _workspace_id = _api_headers(client)
        injected = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "10.00", "workspace_id": "attacker"},
        )
        fractional = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "10.001"},
        )

    assert injected.status_code == 400
    assert "workspace_id" in injected.text
    assert fractional.status_code == 400
    assert "cents" in fractional.text
    assert called is False


def test_x402_fund_enforces_amount_cap_and_rate_limit(monkeypatch) -> None:
    calls = 0

    def create_payment_intent(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _payment_intent(
            payment_intent_id=f"pi_x402_{calls}",
            workspace_id=str(kwargs["metadata"]["workspace_id"]),
            status="requires_action",
            received_cents=None,
        )

    monkeypatch.setattr(
        "trusted_router.services.x402_billing.stripe.PaymentIntent.create",
        create_payment_intent,
    )
    with _x402_client(x402_max_fund_dollars="5", x402_rate_limit_key_per_window=1) as client:
        headers, _workspace_id = _api_headers(client)
        too_large = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "6.00"},
        )
        first = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "5.00"},
        )
        second = client.post(
            "/v1/billing/x402/fund",
            headers=headers,
            json={"amount": "5.00"},
        )

    assert too_large.status_code == 400
    assert first.status_code == 402
    assert second.status_code == 429
    assert calls == 1


def test_x402_settle_credits_succeeded_payment_once(monkeypatch) -> None:
    with _x402_client() as client:
        headers, workspace_id = _api_headers(client)
        before = STORE.get_credit_account(workspace_id)
        assert before is not None
        before_total = before.total_credits_microdollars

        def retrieve(_payment_intent_id: str, **_kwargs: Any) -> dict[str, Any]:
            return _payment_intent(workspace_id=workspace_id)

        monkeypatch.setattr(
            "trusted_router.services.x402_billing.stripe.PaymentIntent.retrieve",
            retrieve,
        )
        first = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_x402_good"},
        )
        second = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_x402_good"},
        )
        account = STORE.get_credit_account(workspace_id)

    assert first.status_code == 200, first.text
    assert first.json()["data"]["credited"] is True
    assert second.status_code == 200, second.text
    assert second.json()["data"]["credited"] is False
    assert account is not None
    assert account.total_credits_microdollars == before_total + 10_000_000


def test_x402_settle_pending_wrong_workspace_and_invalid_payment_intents(monkeypatch) -> None:
    with _x402_client() as client:
        headers, workspace_id = _api_headers(client)
        other_workspace = "other-workspace"
        cases = {
            "pi_pending": _payment_intent(workspace_id=workspace_id, status="processing", received_cents=0),
            "pi_wrong_workspace": _payment_intent(workspace_id=other_workspace),
            "pi_non_x402": _payment_intent(workspace_id=workspace_id, payment_method="checkout"),
            "pi_wrong_currency": _payment_intent(workspace_id=workspace_id, currency="eur"),
            "pi_wrong_asset": _payment_intent(workspace_id=workspace_id, asset_code="eurc"),
            "pi_wrong_network": _payment_intent(workspace_id=workspace_id, network="solana"),
        }

        def retrieve(payment_intent_id: str, **_kwargs: Any) -> dict[str, Any]:
            return cases[payment_intent_id]

        monkeypatch.setattr(
            "trusted_router.services.x402_billing.stripe.PaymentIntent.retrieve",
            retrieve,
        )

        pending = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_pending"},
        )
        wrong_workspace = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_wrong_workspace"},
        )
        non_x402 = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_non_x402"},
        )
        wrong_currency = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_wrong_currency"},
        )
        wrong_asset = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_wrong_asset"},
        )
        wrong_network = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_wrong_network"},
        )

    assert pending.status_code == 200
    assert pending.json()["data"]["credited"] is False
    assert wrong_workspace.status_code == 404
    assert non_x402.status_code == 400
    assert wrong_currency.status_code == 400
    assert wrong_asset.status_code == 400
    assert wrong_network.status_code == 400


def test_x402_settle_rejects_malformed_payment_intent_before_stripe(monkeypatch) -> None:
    called = False

    def retrieve(_payment_intent_id: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        "trusted_router.services.x402_billing.stripe.PaymentIntent.retrieve",
        retrieve,
    )
    with _x402_client() as client:
        headers, _workspace_id = _api_headers(client)
        response = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "not-a-payment-intent"},
        )

    assert response.status_code == 400
    assert called is False


def test_x402_settle_caps_credit_to_requested_amount(monkeypatch) -> None:
    with _x402_client() as client:
        headers, workspace_id = _api_headers(client)
        before = STORE.get_credit_account(workspace_id)
        assert before is not None
        before_total = before.total_credits_microdollars

        def retrieve(_payment_intent_id: str, **_kwargs: Any) -> dict[str, Any]:
            return _payment_intent(
                workspace_id=workspace_id,
                requested_microdollars=5_000_000,
                amount_cents=1000,
                received_cents=1000,
            )

        monkeypatch.setattr(
            "trusted_router.services.x402_billing.stripe.PaymentIntent.retrieve",
            retrieve,
        )
        response = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_x402_good"},
        )
        account = STORE.get_credit_account(workspace_id)

    assert response.status_code == 200
    assert response.json()["data"]["amount_microdollars"] == 5_000_000
    assert account is not None
    assert account.total_credits_microdollars == before_total + 5_000_000


def test_x402_webhook_and_settle_are_idempotent_in_both_orders(monkeypatch) -> None:
    with _x402_client() as client:
        headers, workspace_id = _api_headers(client)
        before = STORE.get_credit_account(workspace_id)
        assert before is not None
        before_total = before.total_credits_microdollars
        payment = _payment_intent(workspace_id=workspace_id)

        first_webhook = client.post(
            "/v1/internal/stripe/webhook",
            json={
                "id": "evt_x402_first",
                "type": "payment_intent.succeeded",
                "data": {"object": payment},
            },
        )
        retry_webhook = client.post(
            "/v1/internal/stripe/webhook",
            json={
                "id": "evt_x402_retry_different_event_id",
                "type": "payment_intent.succeeded",
                "data": {"object": payment},
            },
        )

        def retrieve(_payment_intent_id: str, **_kwargs: Any) -> dict[str, Any]:
            return payment

        monkeypatch.setattr(
            "trusted_router.services.x402_billing.stripe.PaymentIntent.retrieve",
            retrieve,
        )
        settle = client.post(
            "/v1/billing/x402/settle",
            headers=headers,
            json={"payment_intent_id": "pi_x402_good"},
        )
        account = STORE.get_credit_account(workspace_id)

    assert first_webhook.status_code == 200
    assert first_webhook.json()["data"]["credited"] is True
    assert retry_webhook.status_code == 200
    assert retry_webhook.json()["data"]["credited"] is False
    assert settle.status_code == 200
    assert settle.json()["data"]["credited"] is False
    assert account is not None
    assert account.total_credits_microdollars == before_total + 10_000_000


def test_x402_concurrent_settle_credits_once(monkeypatch) -> None:
    with _x402_client(x402_settle_rate_limit_per_window=100) as client:
        headers, workspace_id = _api_headers(client)
        before = STORE.get_credit_account(workspace_id)
        assert before is not None
        before_total = before.total_credits_microdollars

        def retrieve(_payment_intent_id: str, **_kwargs: Any) -> dict[str, Any]:
            return _payment_intent(workspace_id=workspace_id)

        monkeypatch.setattr(
            "trusted_router.services.x402_billing.stripe.PaymentIntent.retrieve",
            retrieve,
        )

        def settle_once() -> bool:
            resp = client.post(
                "/v1/billing/x402/settle",
                headers=headers,
                json={"payment_intent_id": "pi_x402_good"},
            )
            assert resp.status_code == 200, resp.text
            return bool(resp.json()["data"]["credited"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: settle_once(), range(4)))
        account = STORE.get_credit_account(workspace_id)

    assert results.count(True) == 1
    assert account is not None
    assert account.total_credits_microdollars == before_total + 10_000_000


def test_x402_refund_webhook_is_operator_visible_not_silent(client: TestClient) -> None:
    response = client.post(
        "/v1/internal/stripe/webhook",
        json={
            "id": "evt_x402_refund",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_x402_refund",
                    "payment_intent": "pi_x402_good",
                    "metadata": {"payment_method": "x402"},
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["refund_requires_manual_review"] is True


def test_x402_webhook_orphan_payment_intent_stops_retrying() -> None:
    with _x402_client() as client:
        response = client.post(
            "/v1/internal/stripe/webhook",
            json={
                "id": "evt_x402_orphan",
                "type": "payment_intent.succeeded",
                "data": {
                    "object": _payment_intent(
                        payment_intent_id="pi_x402_orphan",
                        workspace_id="missing-workspace",
                    )
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["orphan"] is True
    assert response.json()["data"]["credited"] is False
