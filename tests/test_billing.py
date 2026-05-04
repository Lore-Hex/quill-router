from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE


def test_stripe_event_idempotency(user_headers: dict[str, str], client) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = STORE.credits[workspace_id].total_credits_microdollars
    assert STORE.credit_workspace_once(workspace_id, 5_000_000, "evt_1") is True
    assert STORE.credit_workspace_once(workspace_id, 5_000_000, "evt_1") is False
    assert STORE.credits[workspace_id].total_credits_microdollars == before + 5_000_000


def test_stripe_webhook_route_is_idempotent(user_headers: dict[str, str], client) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = STORE.credits[workspace_id].total_credits_microdollars
    event = {
        "id": "evt_checkout_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1200,
                "metadata": {"workspace_id": workspace_id},
            }
        },
    }
    first = client.post("/v1/internal/stripe/webhook", json=event)
    second = client.post("/v1/internal/stripe/webhook", json=event)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["credited"] is True
    assert second.json()["data"]["credited"] is False
    assert STORE.credits[workspace_id].total_credits_microdollars == before + 12_000_000


def test_billing_checkout_and_portal_mock_without_stripe_secret(user_headers: dict[str, str], client) -> None:
    checkout = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"amount": 25},
    )
    assert checkout.status_code == 201, checkout.text
    checkout_data = checkout.json()["data"]
    assert checkout_data["mode"] == "mock"
    assert checkout_data["amount"] == 25
    assert checkout_data["url"].endswith("/billing/mock-checkout")

    portal = client.post("/v1/billing/portal", headers=user_headers, json={})
    assert portal.status_code == 200
    assert portal.json()["data"]["mode"] == "mock"

    setup = client.post("/v1/billing/payment-methods/setup", headers=user_headers)
    assert setup.status_code == 201, setup.text
    setup_data = setup.json()["data"]
    assert setup_data["mode"] == "mock_setup"
    workspace_id = setup_data["workspace_id"]
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.stripe_customer_id
    assert account.stripe_payment_method_id


def test_payment_method_setup_uses_stripe_setup_mode(monkeypatch, user_headers: dict[str, str]) -> None:
    app = create_app(Settings(environment="test", stripe_secret_key="sk_test_setup"))  # noqa: S106
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_setup", "url": "https://checkout.stripe.test/setup"}

    monkeypatch.setattr("trusted_router.services.stripe_billing.stripe.checkout.Session.create", create_session)

    with TestClient(app) as local_client:
        setup = local_client.post("/v1/billing/payment-methods/setup", headers=user_headers)

    assert setup.status_code == 201, setup.text
    assert setup.json()["data"]["url"] == "https://checkout.stripe.test/setup"
    assert setup.json()["data"]["mode"] == "stripe_setup"
    assert captured["mode"] == "setup"
    assert captured["payment_method_types"] == ["card"]
    assert captured["setup_intent_data"]["metadata"]["workspace_id"] == setup.json()["data"]["workspace_id"]
    assert captured["metadata"]["purpose"] == "payment_method_setup"


def test_setup_intent_succeeded_webhook_saves_payment_method(user_headers: dict[str, str], client) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    event = {
        "id": "evt_setup_intent_1",
        "type": "setup_intent.succeeded",
        "data": {
            "object": {
                "customer": "cus_setup_123",
                "payment_method": "pm_setup_456",
                "metadata": {"workspace_id": workspace_id},
            }
        },
    }

    resp = client.post("/v1/internal/stripe/webhook", json=event)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["setup_saved"] is True
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.stripe_customer_id == "cus_setup_123"
    assert account.stripe_payment_method_id == "pm_setup_456"


def test_setup_checkout_completed_saves_customer_without_crediting_money(
    user_headers: dict[str, str], client
) -> None:
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = STORE.credits[workspace_id].total_credits_microdollars
    event = {
        "id": "evt_checkout_setup_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "setup",
                "customer": "cus_checkout_setup",
                "amount_total": 0,
                "metadata": {"workspace_id": workspace_id},
            }
        },
    }

    resp = client.post("/v1/internal/stripe/webhook", json=event)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["setup_saved"] is True
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.total_credits_microdollars == before
    assert account.stripe_customer_id == "cus_checkout_setup"


def test_billing_portal_uses_saved_customer_without_client_echo(
    monkeypatch, user_headers: dict[str, str]
) -> None:
    app = create_app(Settings(environment="test", stripe_secret_key="sk_test_portal"))  # noqa: S106
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"url": "https://billing.stripe.test/portal"}

    monkeypatch.setattr("trusted_router.services.stripe_billing.stripe.billing_portal.Session.create", create_session)

    with TestClient(app) as local_client:
        workspace_id = local_client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
        STORE.set_stripe_customer(
            workspace_id,
            customer_id="cus_saved_portal",
            payment_method_id="pm_saved_portal",
        )
        portal = local_client.post(
            "/v1/billing/portal",
            headers=user_headers,
            json={"customer_id": "cus_attacker_supplied"},
        )

    assert portal.status_code == 200, portal.text
    assert portal.json()["data"]["url"] == "https://billing.stripe.test/portal"
    assert portal.json()["data"]["mode"] == "stripe"
    assert captured["customer"] == "cus_saved_portal"


def test_billing_portal_rejects_client_supplied_customer_without_saved_customer(
    user_headers: dict[str, str],
) -> None:
    app = create_app(Settings(environment="test", stripe_secret_key="sk_test_portal"))  # noqa: S106

    with TestClient(app) as local_client:
        portal = local_client.post(
            "/v1/billing/portal",
            headers=user_headers,
            json={"customer_id": "cus_attacker_supplied"},
        )

    assert portal.status_code == 400
    assert portal.json()["error"]["type"] == "bad_request"


def test_billing_checkout_validates_amount_and_workspace(user_headers: dict[str, str], client) -> None:
    too_small = client.post("/v1/billing/checkout", headers=user_headers, json={"amount": 0})
    assert too_small.status_code == 400
    assert too_small.json()["error"]["type"] == "bad_request"

    too_large = client.post("/v1/billing/checkout", headers=user_headers, json={"amount": 10001})
    assert too_large.status_code == 400
    assert too_large.json()["error"]["type"] == "bad_request"

    missing_workspace = client.post(
        "/v1/billing/checkout",
        headers=user_headers,
        json={"workspace_id": "missing", "amount": 25},
    )
    assert missing_workspace.status_code == 403
    assert missing_workspace.json()["error"]["type"] == "forbidden"


def test_concurrent_reservations_do_not_overspend(user_headers: dict[str, str], client) -> None:
    key = client.post("/v1/keys", headers=user_headers, json={"name": "reserve"}).json()["data"]
    workspace_id = key["workspace_id"]
    STORE.credits[workspace_id].total_credits_microdollars = 1_000_000
    STORE.credits[workspace_id].total_usage_microdollars = 0
    STORE.credits[workspace_id].reserved_microdollars = 0

    def reserve_once() -> bool:
        try:
            STORE.reserve(workspace_id, key["hash"], 600_000)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reserve_once(), range(4)))
    assert results.count(True) == 1
    assert STORE.credits[workspace_id].reserved_microdollars == 600_000


def test_internal_gateway_authorize_and_settle_records_metadata(
    user_headers: dict[str, str],
    client,
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "gateway"}).json()
    key_hash = created["data"]["hash"]
    workspace_id = created["data"]["workspace_id"]

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 20,
            "max_output_tokens": 4,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]
    assert auth_data["usage_type"] == "Credits"
    assert auth_data["credit_reservation_id"]
    assert auth_data["content_storage_enabled"] is False
    assert STORE.credits[workspace_id].reserved_microdollars > 0

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth_data["authorization_id"],
            "actual_input_tokens": 20,
            "actual_output_tokens": 2,
            "request_id": "gw-req-1",
            "finish_reason": "stop",
            "app": "attested-gateway-test",
            "elapsed_seconds": 0.5,
        },
    )
    assert settle.status_code == 200, settle.text
    generation_id = settle.json()["data"]["generation_id"]
    assert generation_id in STORE.generation_store.generations
    generation = STORE.generation_store.generations[generation_id]
    assert generation.request_id == "gw-req-1"
    assert generation.app == "attested-gateway-test"
    assert STORE.credits[workspace_id].reserved_microdollars == 0
    assert STORE.credits[workspace_id].total_usage_microdollars == generation.total_cost_microdollars

    repeat = client.post(
        "/v1/internal/gateway/settle",
        json={"authorization_id": auth_data["authorization_id"]},
    )
    assert repeat.status_code == 200
    assert repeat.json()["data"]["already_settled"] is True


def test_internal_gateway_byok_uses_configured_secret_ref_and_refunds_key_limit(
    user_headers: dict[str, str],
    client,
) -> None:
    env_ref = "env://" + "CEREBRAS_API_KEY"
    byok = client.put(
        "/v1/byok/providers/cerebras",
        headers=user_headers,
        json={"secret_ref": env_ref, "key_hint": "****9999"},
    )
    assert byok.status_code == 201
    created = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "gateway byok", "limit": 0.01, "include_byok_in_limit": True},
    ).json()
    key_hash = created["data"]["hash"]

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "cerebras/llama3.1-8b",
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 20,
            "max_output_tokens": 4,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth_data = authorize.json()["data"]
    assert auth_data["usage_type"] == "BYOK"
    assert auth_data["byok_secret_ref"] == env_ref
    assert auth_data["byok_key_hint"] == "****9999"
    assert STORE.api_keys.keys[key_hash].reserved_microdollars > 0

    refund = client.post(
        "/v1/internal/gateway/refund",
        json={"authorization_id": auth_data["authorization_id"]},
    )
    assert refund.status_code == 200
    assert STORE.api_keys.keys[key_hash].reserved_microdollars == 0
    assert STORE.api_keys.keys[key_hash].byok_usage_microdollars == 0


def test_internal_gateway_byok_returns_envelope_for_uploaded_raw_key(
    user_headers: dict[str, str],
    client,
    test_settings,
) -> None:
    from trusted_router.byok_crypto import decrypt_byok_secret
    from trusted_router.storage_models import EncryptedSecretEnvelope

    raw_key = "csk-live-user-owned-key-9999"
    byok = client.put(
        "/v1/byok/providers/cerebras",
        headers=user_headers,
        json={"api_key": raw_key},
    )
    assert byok.status_code == 201, byok.text
    created = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "gateway encrypted byok", "limit": 0.01, "include_byok_in_limit": True},
    ).json()

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "cerebras/llama3.1-8b",
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 20,
            "max_output_tokens": 4,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["usage_type"] == "BYOK"
    assert data["byok_secret_ref"].startswith("byok://")
    assert data["byok_key_hint"] == "csk-li...9999"
    assert data["byok_encrypted_secret"]["algorithm"].startswith("TR-BYOK-ENVELOPE")
    assert data["byok_encrypted_secret"]["ciphertext"]
    assert raw_key not in str(data)
    assert decrypt_byok_secret(
        EncryptedSecretEnvelope(**data["byok_encrypted_secret"]),
        test_settings,
        workspace_id=data["workspace_id"],
        provider="cerebras",
    ) == raw_key


def test_internal_gateway_rejects_disabled_key(user_headers: dict[str, str], client) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "disabled gateway"}).json()
    key_hash = created["data"]["hash"]
    assert client.patch(f"/v1/keys/{key_hash}", headers=user_headers, json={"disabled": True}).status_code == 200

    resp = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "openai/gpt-4o-mini",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "unauthorized"
