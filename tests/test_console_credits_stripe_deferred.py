from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.console import credits as credits_module
from trusted_router.storage import STORE, Workspace


def _signed_in_client(
    *,
    email: str = "deferred-stripe@example.com",
    settings: Settings | None = None,
) -> tuple[TestClient, Workspace]:
    app = create_app(
        settings
        or Settings(
            environment="local",
            stripe_secret_key="sk_test_deferred",  # noqa: S106 - test fixture.
        ),
        init_observability=False,
    )
    client = TestClient(app)
    user = STORE.ensure_user(email)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label=email,
        workspace_id=workspace.id,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, workspace


def test_credits_html_finishes_before_stripe_starts_and_keeps_live_money_read(
    monkeypatch,
) -> None:
    client, workspace = _signed_in_client()
    stripe_started = Event()
    release_stripe = Event()
    money_reads: list[str] = []

    def blocking_stripe(**_: Any) -> list[dict[str, Any]]:
        stripe_started.set()
        assert release_stripe.wait(timeout=5)
        return []

    def live_money(workspace_id: str) -> dict[str, int]:
        money_reads.append(workspace_id)
        return {
            "total_credits": 12_340_000,
            "total_usage": 2_340_000,
            "reserved": 0,
            "available": 10_000_000,
        }

    monkeypatch.setattr(credits_module, "list_workspace_payments", blocking_stripe)
    monkeypatch.setattr(credits_module, "describe_saved_payment_method", blocking_stripe)
    monkeypatch.setattr(credits_module, "live_credit_summary", live_money)

    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.get, "/console/credits")
        try:
            response = request.result(timeout=2)
        finally:
            release_stripe.set()

    assert response.status_code == 200
    assert not stripe_started.is_set()
    assert money_reads == [workspace.id]
    assert "$10.00" in response.text
    assert "$2.34" in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "Cookie" in response.headers["vary"].split(", ")
    assert 'data-credits-stripe-details-url="/console/credits/stripe-details"' in response.text


def test_stripe_fragment_populates_masked_card_and_payment_history(monkeypatch) -> None:
    client, workspace = _signed_in_client()
    STORE.set_stripe_customer(
        workspace.id,
        customer_id="cus_deferred",
        payment_method_id="pm_deferred_card",
    )
    seen_workspaces: list[str] = []

    def list_payments(*, workspace_id: str, **_: Any) -> list[dict[str, Any]]:
        seen_workspaces.append(workspace_id)
        return [
            {
                "payment_intent": "pi_deferred",
                "created_at": 1779582083,
                "amount_cents": 1_134,
                "credit_amount_cents": 1_000,
                "processing_fee_cents": 134,
                "currency": "usd",
                "status": "succeeded",
                "payment_status": "paid",
                "receipt_url": "https://pay.stripe.com/receipts/deferred",
                "payment_method_type": "card",
                "card_brand": "visa",
                "card_last4": "4242",
                "bank_name": None,
                "bank_last4": None,
            }
        ]

    monkeypatch.setattr(credits_module, "list_workspace_payments", list_payments)
    monkeypatch.setattr(
        credits_module,
        "describe_saved_payment_method",
        lambda **_: {
            "id_tail": "d_card",
            "brand": "visa",
            "last4": "4242",
            "exp_month": 12,
            "exp_year": 2031,
            "details_available": True,
        },
    )

    response = client.get("/console/credits/stripe-details")

    assert response.status_code == 200
    assert seen_workspaces == [workspace.id]
    assert "Visa card" in response.text
    assert "ending in 4242" in response.text
    assert "expires 12/2031" in response.text
    assert "$10.00" in response.text
    assert "$1.34 fee" in response.text
    assert "2026-05-24" in response.text
    assert "https://pay.stripe.com/receipts/deferred" in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "Cookie" in response.headers["vary"].split(", ")


def test_stripe_fragment_falls_back_when_each_stripe_read_fails(monkeypatch) -> None:
    from trusted_router.services import stripe_billing

    client, workspace = _signed_in_client()
    STORE.set_stripe_customer(
        workspace.id,
        customer_id="cus_fallback",
        payment_method_id="pm_secret_full_identifier",
    )

    def unavailable(**_: Any) -> Any:
        raise RuntimeError("Stripe unavailable")

    monkeypatch.setattr(stripe_billing.stripe.PaymentIntent, "search", unavailable)
    monkeypatch.setattr(stripe_billing.stripe.PaymentMethod, "retrieve", unavailable)

    response = client.get("/console/credits/stripe-details")

    assert response.status_code == 200
    assert "Stripe card details are temporarily unavailable" in response.text
    assert "id ...tifier" in response.text
    assert "pm_secret_full_identifier" not in response.text
    assert "Payment history is temporarily unavailable" in response.text


def test_stripe_fragment_loads_card_and_history_concurrently(monkeypatch) -> None:
    client, workspace = _signed_in_client()
    STORE.set_stripe_customer(
        workspace.id,
        customer_id="cus_parallel",
        payment_method_id="pm_parallel",
    )
    history_started = Event()
    card_started = Event()

    def list_payments(**_: Any) -> list[dict[str, Any]]:
        history_started.set()
        assert card_started.wait(timeout=3)
        return [
            {
                "amount_cents": 100,
                "currency": "usd",
                "payment_status": "paid",
                "status": "succeeded",
                "card_brand": "parallel-history",
                "card_last4": "0001",
            }
        ]

    def describe_method(**_: Any) -> dict[str, Any]:
        card_started.set()
        assert history_started.wait(timeout=3)
        return {
            "id_tail": "rallel",
            "brand": "parallel-card",
            "last4": "0002",
            "exp_month": 1,
            "exp_year": 2032,
            "details_available": True,
        }

    monkeypatch.setattr(credits_module, "list_workspace_payments", list_payments)
    monkeypatch.setattr(
        credits_module,
        "describe_saved_payment_method",
        describe_method,
    )

    response = client.get("/console/credits/stripe-details")

    assert response.status_code == 200
    assert "Parallel-card card" in response.text
    assert "parallel-history" in response.text


def test_stripe_fragment_requires_session_before_any_stripe_work(monkeypatch) -> None:
    app = create_app(
        Settings(
            environment="local",
            stripe_secret_key="sk_test_deferred",  # noqa: S106 - test fixture.
        ),
        init_observability=False,
    )
    client = TestClient(app)
    stripe_started = Event()

    def should_not_run(**_: Any) -> Any:
        stripe_started.set()
        raise AssertionError("Stripe ran before console authentication")

    monkeypatch.setattr(credits_module, "list_workspace_payments", should_not_run)
    monkeypatch.setattr(credits_module, "describe_saved_payment_method", should_not_run)

    response = client.get(
        "/console/credits/stripe-details",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/?reason=signin"
    assert not stripe_started.is_set()


def test_stripe_fragment_is_scoped_to_workspace_from_each_session(monkeypatch) -> None:
    settings = Settings(
        environment="local",
        stripe_secret_key="sk_test_deferred",  # noqa: S106 - test fixture.
    )
    app = create_app(settings, init_observability=False)

    def client_for(email: str) -> tuple[TestClient, Workspace]:
        client = TestClient(app)
        user = STORE.ensure_user(email)
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        STORE.set_stripe_customer(
            workspace.id,
            customer_id=f"cus_{workspace.id}",
            payment_method_id=f"pm_{workspace.id}",
        )
        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="email",
            label=email,
            workspace_id=workspace.id,
            ttl_seconds=3600,
            state="active",
        )
        client.cookies.set("tr_session", raw_token)
        return client, workspace

    alice, alice_workspace = client_for("alice-deferred@example.com")
    bob, bob_workspace = client_for("bob-deferred@example.com")
    markers = {
        alice_workspace.id: "alice-only-marker",
        bob_workspace.id: "bob-only-marker",
    }

    def scoped_payments(*, workspace_id: str, **_: Any) -> list[dict[str, Any]]:
        return [
            {
                "amount_cents": 100,
                "currency": "usd",
                "payment_status": "paid",
                "status": "succeeded",
                "card_brand": markers[workspace_id],
                "card_last4": "0001",
            }
        ]

    monkeypatch.setattr(credits_module, "list_workspace_payments", scoped_payments)
    monkeypatch.setattr(
        credits_module,
        "describe_saved_payment_method",
        lambda **_: None,
    )

    alice_response = alice.get("/console/credits/stripe-details")
    bob_response = bob.get("/console/credits/stripe-details")

    assert markers[alice_workspace.id] in alice_response.text
    assert markers[bob_workspace.id] not in alice_response.text
    assert markers[bob_workspace.id] in bob_response.text
    assert markers[alice_workspace.id] not in bob_response.text


def test_blocked_stripe_fragment_does_not_stall_event_loop(monkeypatch) -> None:
    client, _workspace = _signed_in_client()
    stripe_started = Event()
    release_stripe = Event()

    def blocking_list(**_: Any) -> list[dict[str, Any]]:
        stripe_started.set()
        assert release_stripe.wait(timeout=5)
        return []

    monkeypatch.setattr(credits_module, "list_workspace_payments", blocking_list)
    monkeypatch.setattr(
        credits_module,
        "describe_saved_payment_method",
        lambda **_: None,
    )

    with TestClient(client.app) as concurrent_client:
        concurrent_client.cookies.update(client.cookies)
        with ThreadPoolExecutor(max_workers=1) as pool:
            fragment = pool.submit(
                concurrent_client.get,
                "/console/credits/stripe-details",
            )
            assert stripe_started.wait(timeout=3)
            started = time.perf_counter()
            try:
                health = concurrent_client.get("/health")
                elapsed = time.perf_counter() - started
            finally:
                release_stripe.set()
            fragment_response = fragment.result(timeout=3)

    assert health.status_code == 200
    assert fragment_response.status_code == 200
    assert elapsed < 0.75
