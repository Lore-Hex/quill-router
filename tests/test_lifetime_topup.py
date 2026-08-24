from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.paypal_billing import credit_paypal_capture
from trusted_router.services.x402_billing import credit_x402_payment_intent
from trusted_router.storage import STORE


def _user_and_workspace(client: TestClient, headers: dict[str, str]) -> tuple[Any, Any]:
    response = client.get("/v1/workspaces", headers=headers)
    assert response.status_code == 200, response.text
    workspace = STORE.get_workspace(response.json()["data"][0]["id"])
    user = STORE.find_user_by_email(headers["x-trustedrouter-user"])
    assert user is not None and workspace is not None
    return user, workspace


def _stripe_client() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                environment="test",
                stripe_secret_key="sk_test_lifetime",  # noqa: S106
                stripe_webhook_secret=None,
            ),
            init_observability=False,
        )
    )


def test_stripe_checkout_stamps_initiator_and_replay_accrues_once(
    monkeypatch,
    user_headers: dict[str, str],
) -> None:
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_lifetime", "url": "https://stripe.test/checkout"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )
    with _stripe_client() as client:
        checkout = client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 25, "payment_method": "card"},
        )
        user, workspace = _user_and_workspace(client, user_headers)
        metadata = captured["metadata"]
        assert metadata["initiating_user_id"] == user.id
        event = {
            "id": "evt_lifetime_card",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": int(metadata["charge_amount_cents"]),
                    "metadata": metadata,
                }
            },
        }
        first = client.post("/v1/internal/stripe/webhook", json=event)
        replay = client.post("/v1/internal/stripe/webhook", json=event)

    assert checkout.status_code == 201, checkout.text
    assert first.json()["data"]["credited"] is True
    assert replay.json()["data"]["credited"] is False
    assert STORE.get_lifetime_topup_microdollars(user.id) == 25_000_000
    assert STORE.get_workspace(workspace.id) is not None


def test_legacy_stripe_ach_event_falls_back_to_workspace_owner(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    owner, workspace = _user_and_workspace(client, user_headers)
    event = {
        "id": "evt_lifetime_ach",
        "type": "checkout.session.async_payment_succeeded",
        "data": {
            "object": {
                "id": "cs_legacy_ach",
                "payment_intent": "pi_legacy_ach",
                "amount_total": 300,
                "metadata": {
                    "workspace_id": workspace.id,
                    "payment_method": "ach",
                },
            }
        },
    }

    first = client.post("/v1/internal/stripe/webhook", json=event)
    replay = client.post("/v1/internal/stripe/webhook", json=event)

    assert first.status_code == 200, first.text
    assert replay.json()["data"]["credited"] is False
    assert STORE.get_lifetime_topup_microdollars(owner.id) == 3_000_000


def test_auto_refill_webhook_accrues_to_workspace_owner_without_initiator(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    owner, workspace = _user_and_workspace(client, user_headers)
    event = {
        "id": "evt_lifetime_auto_refill",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_lifetime_auto_refill",
                "amount": 200,
                "metadata": {
                    "workspace_id": workspace.id,
                    "auto_refill": "true",
                    "amount_microdollars": "2000000",
                },
            }
        },
    }

    response = client.post("/v1/internal/stripe/webhook", json=event)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["credited"] is True
    assert STORE.get_lifetime_topup_microdollars(owner.id) == 2_000_000


def _paypal_order(
    workspace_id: str,
    custom_id: str,
    *,
    capture_id: str,
    amount: str,
) -> dict[str, Any]:
    return {
        "id": f"ORDER-{capture_id}",
        "purchase_units": [
            {
                "custom_id": custom_id,
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": "COMPLETED",
                            "amount": {"currency_code": "USD", "value": amount},
                        }
                    ]
                },
            }
        ],
    }


def test_paypal_accrues_to_initiating_user_and_legacy_order_falls_back_to_owner(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    owner, workspace = _user_and_workspace(client, user_headers)
    initiator = STORE.ensure_user("paypal-initiator@example.com")
    current = _paypal_order(
        workspace.id,
        json.dumps({"w": workspace.id, "u": initiator.id, "c": 2500, "t": 2500}),
        capture_id="CAPTURE-INITIATOR",
        amount="25.00",
    )
    legacy = _paypal_order(
        workspace.id,
        workspace.id,
        capture_id="CAPTURE-LEGACY-OWNER",
        amount="3.00",
    )

    first = credit_paypal_capture(current, expected_workspace_id=workspace.id)
    replay = credit_paypal_capture(current, expected_workspace_id=workspace.id)
    credit_paypal_capture(legacy, expected_workspace_id=workspace.id)

    assert first.credited is True
    assert replay.credited is False
    assert STORE.get_lifetime_topup_microdollars(initiator.id) == 25_000_000
    assert STORE.get_lifetime_topup_microdollars(owner.id) == 3_000_000


def _x402_intent(
    workspace_id: str,
    payment_intent_id: str,
    *,
    initiating_user_id: str | None,
) -> dict[str, Any]:
    metadata = {
        "workspace_id": workspace_id,
        "amount_microdollars": "1000000",
        "payment_method": "x402",
        "purpose": "trustedrouter_credits",
        "asset": "USDC",
        "network": "base",
    }
    if initiating_user_id is not None:
        metadata["initiating_user_id"] = initiating_user_id
    return {
        "id": payment_intent_id,
        "status": "succeeded",
        "currency": "usd",
        "amount": 100,
        "amount_received": 100,
        "metadata": metadata,
    }


def test_x402_accrues_to_initiator_and_legacy_intent_falls_back_to_owner(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    owner, workspace = _user_and_workspace(client, user_headers)
    initiator = STORE.ensure_user("x402-initiator@example.com")
    settings = Settings(environment="test", x402_enabled=True)
    current = _x402_intent(
        workspace.id,
        "pi_x402_lifetime_initiator",
        initiating_user_id=initiator.id,
    )
    legacy = _x402_intent(
        workspace.id,
        "pi_x402_lifetime_owner",
        initiating_user_id=None,
    )

    first = credit_x402_payment_intent(
        current,
        expected_workspace_id=workspace.id,
        settings=settings,
    )
    replay = credit_x402_payment_intent(
        current,
        expected_workspace_id=workspace.id,
        settings=settings,
    )
    credit_x402_payment_intent(
        legacy,
        expected_workspace_id=workspace.id,
        settings=settings,
    )

    assert first["credited"] is True
    assert replay["credited"] is False
    assert STORE.get_lifetime_topup_microdollars(initiator.id) == 1_000_000
    assert STORE.get_lifetime_topup_microdollars(owner.id) == 1_000_000


def test_signup_credit_never_counts_as_a_lifetime_topup(client: TestClient) -> None:
    response = client.post(
        "/v1/signup",
        json={"email": "lifetime-signup@example.com"},
    )

    assert response.status_code == 201, response.text
    user = STORE.find_user_by_email("lifetime-signup@example.com")
    assert user is not None
    assert response.json()["data"]["trial_credit_microdollars"] > 0
    assert STORE.get_lifetime_topup_microdollars(user.id) == 0


def test_identity_checkout_nudge_is_present_only_for_that_purpose(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    user, workspace = _user_and_workspace(client, user_headers)
    ordinary = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": 1},
    )
    identity = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": 1, "purpose": "identity_verification"},
    )
    STORE.credit_workspace_typed_direct(
        workspace.id,
        5_000_000,
        "evt_nudge_existing_topup",
        lifetime_topup_user_id=user.id,
    )
    progressed = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": 1, "purpose": "identity_verification"},
    )
    too_small = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": "0.99", "purpose": "identity_verification"},
    )

    assert ordinary.status_code == 201
    assert "verification_topup_remaining" not in ordinary.json()["data"]
    assert identity.json()["data"]["verification_topup_remaining"] == 25
    assert identity.json()["data"]["verification_topup_remaining_microdollars"] == 25_000_000
    assert progressed.json()["data"]["verification_topup_remaining"] == 20
    assert too_small.status_code == 400
