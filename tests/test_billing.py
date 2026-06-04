from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import DEFAULT_TRIAL_CREDIT_MICRODOLLARS
from trusted_router.security import lookup_hash_api_key
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


def test_setup_checkout_completed_saves_customer_and_grants_trial_credit(
    user_headers: dict[str, str], client
) -> None:
    """A successful Stripe Checkout in `mode=setup` (saved-card capture
    with no charge) is the moment we know the user has a Stripe-validated
    card. Policy: grant the standard trial credit at this moment, not at
    signup. The test resets the workspace credit + dedup ledger entry to
    a pre-card-attach state first to override the conftest
    auto_credit_test_workspaces fixture (which simulates "card already
    attached" for the rest of the suite)."""
    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    # Force pre-card-attach state — undo the conftest test-only auto-credit.
    # Clearing both the credit balance AND the per-workspace "trial" event-id
    # in the dedup ledger so the webhook's idempotent credit_workspace_once
    # actually fires (otherwise it'd see the conftest fixture's grant as a
    # prior trial-grant and no-op).
    STORE.credits[workspace_id].total_credits_microdollars = 0
    STORE.stripe_events.discard(f"trial:{workspace_id}")
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
    body = resp.json()["data"]
    assert body["setup_saved"] is True
    assert body["trial_credit_granted_microdollars"] == DEFAULT_TRIAL_CREDIT_MICRODOLLARS
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.total_credits_microdollars == DEFAULT_TRIAL_CREDIT_MICRODOLLARS
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


def test_internal_gateway_authorize_replays_same_idempotency_key_once(
    user_headers: dict[str, str],
    client,
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "gateway"}).json()
    key_hash = created["data"]["hash"]
    workspace_id = created["data"]["workspace_id"]
    body = {
        "api_key_hash": key_hash,
        "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 20,
        "max_output_tokens": 4,
    }

    first = client.post(
        "/v1/internal/gateway/authorize",
        headers={"idempotency-key": "idem-gateway-1"},
        json=body,
    )
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    first_reserved = STORE.credits[workspace_id].reserved_microdollars

    repeat = client.post(
        "/v1/internal/gateway/authorize",
        headers={"idempotency-key": "idem-gateway-1"},
        json=body,
    )
    assert repeat.status_code == 200, repeat.text
    repeat_data = repeat.json()["data"]
    assert repeat_data["authorization_id"] == first_data["authorization_id"]
    assert repeat_data["credit_reservation_id"] == first_data["credit_reservation_id"]
    assert repeat_data["idempotent_replay"] is True
    assert STORE.credits[workspace_id].reserved_microdollars == first_reserved


def test_internal_gateway_authorize_rejects_idempotency_key_body_mismatch(
    user_headers: dict[str, str],
    client,
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "gateway"}).json()
    key_hash = created["data"]["hash"]
    body = {
        "api_key_hash": key_hash,
        "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 20,
        "max_output_tokens": 4,
    }
    first = client.post(
        "/v1/internal/gateway/authorize",
        headers={"idempotency-key": "idem-gateway-conflict"},
        json=body,
    )
    assert first.status_code == 200, first.text

    conflict = client.post(
        "/v1/internal/gateway/authorize",
        headers={"idempotency-key": "idem-gateway-conflict"},
        json={**body, "max_output_tokens": 8},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["type"] == "conflict"


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
            "model": "meta-llama/llama-3.1-8b-instruct",
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
            "model": "meta-llama/llama-3.1-8b-instruct",
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
    assert data["byok_cache_key"].startswith("byokcache:v1:")
    assert data["byok_encrypted_secret"]["algorithm"].startswith("TR-BYOK-ENVELOPE")
    assert data["byok_encrypted_secret"]["ciphertext"]
    assert data["route_candidates"][0]["byok_cache_key"] == data["byok_cache_key"]
    assert raw_key not in str(data)
    assert decrypt_byok_secret(
        EncryptedSecretEnvelope(**data["byok_encrypted_secret"]),
        test_settings,
        workspace_id=data["workspace_id"],
        provider="cerebras",
    ) == raw_key


def test_internal_gateway_byok_cache_key_changes_on_rotation(
    user_headers: dict[str, str],
    client,
) -> None:
    first_key = "csk-live-user-owned-key-1111"
    rotated_key = "csk-live-user-owned-key-2222"
    assert client.put(
        "/v1/byok/providers/cerebras",
        headers=user_headers,
        json={"api_key": first_key},
    ).status_code == 201
    created = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "gateway byok rotation", "limit": 0.02, "include_byok_in_limit": True},
    ).json()

    def authorize_cache_key() -> str:
        resp = client.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": created["data"]["hash"],
                "model": "meta-llama/llama-3.1-8b-instruct",
                "provider": {"usage": "byok"},
                "estimated_input_tokens": 1,
                "max_output_tokens": 1,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["byok_cache_key"].startswith("byokcache:v1:")
        assert rotated_key not in str(data)
        assert first_key not in str(data)
        return data["byok_cache_key"]

    first_cache_key = authorize_cache_key()
    assert client.put(
        "/v1/byok/providers/cerebras",
        headers=user_headers,
        json={"api_key": rotated_key},
    ).status_code == 200

    assert authorize_cache_key() != first_cache_key
    assert client.delete("/v1/byok/providers/cerebras", headers=user_headers).status_code == 200
    deleted = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": "meta-llama/llama-3.1-8b-instruct",
            "provider": {"usage": "byok"},
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )
    assert deleted.status_code == 400
    assert deleted.json()["error"]["type"] == "provider_not_supported"


def test_internal_gateway_authorizes_by_lookup_hash_without_raw_key(
    user_headers: dict[str, str],
    client,
) -> None:
    created = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "gateway lookup"},
    ).json()
    raw_key = created["key"]

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash_api_key(raw_key),
            "model": "openai/gpt-5.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )

    assert authorize.status_code == 200, authorize.text
    data = authorize.json()["data"]
    assert data["api_key_hash"] == created["data"]["hash"]
    assert raw_key not in str(data)


def test_internal_gateway_rejects_disabled_key(user_headers: dict[str, str], client) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "disabled gateway"}).json()
    key_hash = created["data"]["hash"]
    assert client.patch(f"/v1/keys/{key_hash}", headers=user_headers, json={"disabled": True}).status_code == 200

    resp = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "openai/gpt-5.5",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "unauthorized"


def test_stripe_webhook_handles_stripe_object_from_construct_event(
    user_headers: dict[str, str], client, monkeypatch
) -> None:
    """REGRESSION: 2026-05-23/24 production outage where the live Stripe SDK's
    `stripe.Webhook.construct_event` returned a `stripe.Event` (StripeObject
    subclass) instead of a dict, and newer Stripe SDK versions removed the
    `.get()` method from StripeObject. The handler's first line after
    signature verification was `event.get("id")` — that AttributeError
    bubbled all the way up to 500 BEFORE any credit_workspace_once call,
    so Stripe payments stopped crediting workspaces. Gabriella's $5+$2
    and the post-rotation $1 chain-test both blew up here.

    This test reproduces the exact scenario: stripe_webhook_secret IS set
    (so the verify-signature branch runs), and `construct_event` is
    monkeypatched to return a StripeObject. If we use dict semantics
    (.get with defaults, nested dict access) directly on a StripeObject,
    the handler crashes. The fix is to call `.to_dict_recursive()` once
    and operate on the resulting dict.
    """
    from trusted_router.routes.internal import webhook as webhook_module

    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    before = STORE.credits[workspace_id].total_credits_microdollars

    # Build a real stripe.Event so we exercise the actual StripeObject code
    # path that bit us in prod (rather than a fake "dict-without-.get"
    # which wouldn't catch the SDK's exact failure mode).
    import stripe

    event_payload = {
        "id": "evt_stripeobj_regression_1",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "object": "checkout.session",
                "amount_total": 100,  # $1.00 == 100_000 microdollars
                "customer": "cus_regression",
                "mode": "payment",
                "payment_status": "paid",
                "status": "complete",
                "metadata": {"workspace_id": workspace_id},
            }
        },
    }

    # `stripe.Event.construct_from` builds a real StripeObject from a dict
    # — exactly what Webhook.construct_event would return after signature
    # verification in production. This is the type that broke .get().
    real_stripe_object = stripe.Event.construct_from(event_payload, "sk_test_dummy")
    # Sanity-check that the StripeObject we built reproduces the prod
    # crash mode if used dict-style without conversion.
    assert not hasattr(real_stripe_object, "get") or callable(
        getattr(real_stripe_object, "get", None)
    ), "stripe.Event shape changed; rewrite this test"

    def fake_construct_event(raw, sig, secret):  # type: ignore[no-untyped-def]
        return real_stripe_object

    monkeypatch.setattr(
        webhook_module.stripe.Webhook, "construct_event", fake_construct_event
    )

    # Force the signature-verify branch by setting a webhook secret on the
    # already-running test app's settings. The handler reads
    # settings.stripe_webhook_secret each call so this takes effect
    # immediately without rebuilding the app.
    test_settings = client.app.state.settings
    monkeypatch.setattr(test_settings, "stripe_webhook_secret", "whsec_test_dummy")

    resp = client.post(
        "/v1/internal/stripe/webhook",
        content=b'{"signed":"payload"}',
        headers={
            "stripe-signature": "t=1,v1=irrelevant_because_monkeypatched",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 200, (
        f"webhook handler crashed handling a real StripeObject; "
        f"this is the prod 500 regression: {resp.text}"
    )
    body = resp.json()["data"]
    assert body["credited"] is True, body
    assert body["event_id"] == "evt_stripeobj_regression_1"
    # Verify the credit actually landed — the whole point of fixing this is
    # that real Stripe payments grant credit again.
    after = STORE.credits[workspace_id].total_credits_microdollars
    assert after - before == 100 * 10000, f"expected +$1.00 in microdollars, got {after-before}"


def test_list_workspace_payments_returns_empty_without_stripe_key() -> None:
    """If the deployment has no Stripe secret configured (local/test
    environments), the credits page must still render — return [] and
    let the template show the 'no payments yet' fallback."""
    from trusted_router.config import Settings
    from trusted_router.services.stripe_billing import list_workspace_payments

    result = list_workspace_payments(
        workspace_id="ws-test",
        settings=Settings(environment="local"),
    )
    assert result == []


def test_list_workspace_payments_swallows_stripe_errors(monkeypatch) -> None:
    """Stripe API failures must not block the credits page from rendering.
    A 5xx from Stripe, a network blip, or a search-query quota hit all
    collapse to an empty list — the rest of the page (balance, payment
    methods, auto-refill) still renders. We deliberately don't surface
    the Stripe error to the user; this is a read-only display panel and
    they can refresh."""
    from trusted_router.config import Settings
    from trusted_router.services import stripe_billing

    # `local` env has no fail-closed validator; setting stripe_secret_key
    # is what makes list_workspace_payments take the Stripe-API code
    # path rather than returning [] early.
    settings = Settings(
        environment="local",
        stripe_secret_key="sk_test_dummy",  # noqa: S106
    )

    def explode(**_: Any) -> None:
        raise RuntimeError("simulated Stripe outage")

    monkeypatch.setattr(stripe_billing.stripe.PaymentIntent, "search", explode)
    result = stripe_billing.list_workspace_payments(
        workspace_id="ws-anything",
        settings=settings,
    )
    assert result == []


def _console_client_for_test() -> tuple[TestClient, str]:
    """Build a TestClient with a real active console session cookie.

    The default `client` fixture in conftest authenticates via API-key
    Bearer headers, which the /console/* routes reject — they require
    the SESSION COOKIE that OAuth sign-in mints. This helper stands up
    a parallel client with a session cookie set, so server-rendered
    console pages render their authenticated body instead of bouncing
    to /?reason=signin."""
    from trusted_router.config import Settings
    from trusted_router.main import create_app

    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("billing-test@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="billing-test@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, raw_token


def test_credits_page_renders_payment_history_section(monkeypatch) -> None:
    """The credits page must include a 'Payment history' section, even
    when there are no payments yet. The 2026-05-24 user feedback was
    'shouldn't the credits show a list of every payment I've made?' —
    just showing a balance number isn't enough. Empty-state copy guides
    them rather than silently omitting the section."""
    from trusted_router.routes.console import credits as credits_module

    monkeypatch.setattr(
        credits_module,
        "list_workspace_payments",
        lambda **_: [],
    )
    client, _ = _console_client_for_test()
    resp = client.get("/console/credits")
    assert resp.status_code == 200, resp.text[:300]
    assert "Payment history" in resp.text
    assert "No payments yet" in resp.text


def test_credits_page_renders_payment_rows_when_present(monkeypatch) -> None:
    """When Stripe returns past payments, the credits page renders one
    row per payment with date, amount, card brand+last4, status, and a
    receipt link. This is what the user asked for after the $1 test —
    'shouldn't the credits show a list of every payment I've made?'"""
    # Patch on the credits route module (which imported the function at
    # module load) — patching the services module is too late since the
    # route already has its own reference.
    from trusted_router.routes.console import credits as credits_module

    fake_payments = [
        {
            "payment_intent": "pi_3TaPnB",
            "created_at": 1779582083,  # 2026-05-24 01:01 UTC
            "amount_cents": 100,
            "currency": "usd",
            "status": "succeeded",
            "payment_status": "paid",
            "receipt_url": "https://pay.stripe.com/receipts/test-receipt",
            "card_brand": "visa",
            "card_last4": "4242",
        },
        {
            "payment_intent": "pi_5dollar",
            "created_at": 1779561182,  # 2026-05-23 19:13 UTC
            "amount_cents": 500,
            "currency": "usd",
            "status": "succeeded",
            "payment_status": "paid",
            "receipt_url": "https://pay.stripe.com/receipts/older-receipt",
            "card_brand": "mastercard",
            "card_last4": "8888",
        },
    ]
    monkeypatch.setattr(
        credits_module,
        "list_workspace_payments",
        lambda **_: fake_payments,
    )

    client, _ = _console_client_for_test()
    resp = client.get("/console/credits")
    assert resp.status_code == 200, resp.text[:300]
    assert "Payment history" in resp.text
    # Amount formatted as dollars
    assert "$1.00" in resp.text
    assert "$5.00" in resp.text
    # Card brand + last4 displayed
    assert "visa" in resp.text.lower()
    assert "4242" in resp.text
    assert "mastercard" in resp.text.lower()
    assert "8888" in resp.text
    # Receipt links rendered
    assert "https://pay.stripe.com/receipts/test-receipt" in resp.text
    # `paid` status pill
    assert ">paid<" in resp.text or 'pill good">paid' in resp.text
    # Date formatted (UTC)
    assert "2026-05-24" in resp.text
    assert "2026-05-23" in resp.text
    # Empty-state copy NOT shown when payments exist
    assert "No payments yet" not in resp.text
