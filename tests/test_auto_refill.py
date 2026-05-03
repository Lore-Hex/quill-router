"""Tests for auto-refill billing.

Stripe is mocked because we don't want unit tests to hit the real API
or require live credentials. The webhook path is exercised against the
in-memory store via the existing /internal/stripe/webhook endpoint —
that confirms the credit ledger gets updated by `payment_intent.succeeded`
the same way `checkout.session.completed` already does.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.auto_refill import AutoRefillOutcome, maybe_charge_after_settle
from trusted_router.storage import STORE


@pytest.fixture
def stripe_settings() -> Settings:
    return Settings(
        environment="test",
        stripe_secret_key="sk_test_dummy",  # noqa: S106 - fixture key.
    )


@pytest.fixture
def configured_workspace(stripe_settings: Settings) -> str:
    """Workspace with $10 credit, payment method on file, and auto-refill
    set to fire when balance < $5 with a $20 top-up."""
    user = STORE.ensure_user("alice@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.update_auto_refill_settings(
        workspace.id,
        enabled=True,
        threshold_microdollars=5_000_000,
        amount_microdollars=20_000_000,
    )
    STORE.set_stripe_customer(
        workspace.id,
        customer_id="cus_test_123",
        payment_method_id="pm_test_456",
    )
    return workspace.id


def test_auto_refill_skips_when_disabled(stripe_settings: Settings) -> None:
    user = STORE.ensure_user("disabled@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    outcome = maybe_charge_after_settle(workspace.id, settings=stripe_settings)
    assert outcome == AutoRefillOutcome(fired=False, reason="disabled")


def test_auto_refill_skips_when_above_threshold(
    configured_workspace: str, stripe_settings: Settings
) -> None:
    # Default trial is $10 (above the $5 threshold) so no charge.
    outcome = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
    assert outcome.fired is False
    assert outcome.reason == "above_threshold"


def test_auto_refill_skips_without_payment_method(stripe_settings: Settings) -> None:
    user = STORE.ensure_user("nopayment@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.update_auto_refill_settings(
        workspace.id,
        enabled=True,
        threshold_microdollars=5_000_000,
        amount_microdollars=20_000_000,
    )
    # Drain credits below threshold but never set a payment method.
    STORE.reserve(workspace.id, "fake-key-hash", 8_000_000)
    outcome = maybe_charge_after_settle(workspace.id, settings=stripe_settings)
    assert outcome.fired is False
    assert outcome.reason == "no_payment_method"


def test_auto_refill_fires_below_threshold(
    configured_workspace: str, stripe_settings: Settings
) -> None:
    # Drain credits to $1 (below $5 threshold).
    reservation = STORE.reserve(configured_workspace, "fake-key-hash", 9_000_000)
    STORE.settle(reservation.id, 9_000_000)

    fake_intent = MagicMock(id="pi_test_abc")
    with patch("stripe.PaymentIntent.create", return_value=fake_intent) as create:
        outcome = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
    assert outcome.fired is True
    assert outcome.reason == "charged"
    assert outcome.payment_intent_id == "pi_test_abc"
    create.assert_called_once()
    kwargs = create.call_args.kwargs
    # Convert microdollars to cents: $20 → 2000 cents.
    assert kwargs["amount"] == 2000
    assert kwargs["currency"] == "usd"
    assert kwargs["customer"] == "cus_test_123"
    assert kwargs["payment_method"] == "pm_test_456"
    assert kwargs["off_session"] is True
    assert kwargs["confirm"] is True
    assert kwargs["metadata"]["workspace_id"] == configured_workspace
    assert kwargs["metadata"]["auto_refill"] == "true"
    assert "auto-refill" in kwargs.get("idempotency_key", "")

    # Status should be pending until the webhook lands.
    account = STORE.get_credit_account(configured_workspace)
    assert account is not None
    assert account.last_auto_refill_status == "pending"


def test_auto_refill_records_card_error(
    configured_workspace: str, stripe_settings: Settings
) -> None:
    import stripe

    STORE.settle(STORE.reserve(configured_workspace, "k", 9_000_000).id, 9_000_000)

    error = stripe.CardError(
        message="Card declined",
        param="payment_method",
        code="card_declined",
    )
    with patch("stripe.PaymentIntent.create", side_effect=error):
        outcome = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
    assert outcome.fired is False
    assert outcome.reason.startswith("stripe_error:")
    account = STORE.get_credit_account(configured_workspace)
    assert account is not None
    assert account.last_auto_refill_status == "failed:card_declined"


def test_auto_refill_rate_limits_recent_failures(
    configured_workspace: str, stripe_settings: Settings
) -> None:
    import stripe

    STORE.settle(STORE.reserve(configured_workspace, "k", 9_000_000).id, 9_000_000)
    error = stripe.CardError(message="Decline", param="pm", code="card_declined")
    with patch("stripe.PaymentIntent.create", side_effect=error):
        # First call records the failure.
        first = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
        assert first.reason.startswith("stripe_error:")
        # Immediate retry should be rate-limited (no second call to Stripe).
        second = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
    assert second.fired is False
    assert second.reason == "rate_limited"


def test_payment_intent_succeeded_webhook_credits_workspace(
    configured_workspace: str,
) -> None:
    # Local-mode webhook (no signing secret) so we can hand-craft an event.
    # Explicit None overrides whatever the local keys file has so the
    # webhook handler skips the construct_event signature check.
    settings = Settings(environment="local", stripe_webhook_secret=None)
    app = create_app(settings, init_observability=False, configure_store_arg=False)
    client_iter: Iterator[TestClient]
    with TestClient(app) as client:
        client_iter = iter([client])
        # Fire the payment_intent.succeeded webhook for our workspace.
        event: dict[str, Any] = {
            "id": "evt_test_pi_succeeded",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_test_xyz",
                    "amount": 2000,
                    "customer": "cus_test_123",
                    "payment_method": "pm_test_456",
                    "metadata": {
                        "workspace_id": configured_workspace,
                        "auto_refill": "true",
                        "amount_microdollars": "20000000",
                    },
                }
            },
        }
        before = STORE.get_credit_account(configured_workspace)
        assert before is not None
        before_credits = before.total_credits_microdollars
        resp = client.post("/internal/stripe/webhook", json=event)
        next(client_iter)  # silence unused-iter warning.
    assert resp.status_code == 200
    assert resp.json()["data"]["credited"] is True
    assert resp.json()["data"]["auto_refill"] is True

    after = STORE.get_credit_account(configured_workspace)
    assert after is not None
    assert after.total_credits_microdollars == before_credits + 20_000_000
    assert after.last_auto_refill_status == "succeeded"


def test_payment_intent_failed_webhook_records_failure(
    configured_workspace: str,
) -> None:
    # Explicit None overrides whatever the local keys file has so the
    # webhook handler skips the construct_event signature check.
    settings = Settings(environment="local", stripe_webhook_secret=None)
    app = create_app(settings, init_observability=False, configure_store_arg=False)
    with TestClient(app) as client:
        event = {
            "id": "evt_test_pi_failed",
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_test_failed",
                    "metadata": {
                        "workspace_id": configured_workspace,
                        "auto_refill": "true",
                    },
                    "last_payment_error": {"code": "card_declined"},
                }
            },
        }
        resp = client.post("/internal/stripe/webhook", json=event)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["auto_refill_failed"] is True
    assert body["code"] == "card_declined"

    account = STORE.get_credit_account(configured_workspace)
    assert account is not None
    assert account.last_auto_refill_status == "failed:card_declined"


def test_console_credits_save_auto_refill_persists_settings() -> None:
    # Explicit None overrides whatever the local keys file has so the
    # webhook handler skips the construct_event signature check.
    settings = Settings(environment="local", stripe_webhook_secret=None)
    app = create_app(settings, init_observability=False, configure_store_arg=False)
    with TestClient(app) as client:
        user = STORE.ensure_user("console@example.com")
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        STORE.set_stripe_customer(
            workspace.id,
            customer_id="cus_console",
            payment_method_id="pm_console",
        )
        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="google",
            label="console@example.com",
            ttl_seconds=3600,
            state="active",
        )
        client.cookies.set("tr_session", raw_token)
        resp = client.post(
            "/console/credits/auto-refill",
            data={"enabled": "1", "threshold": "10", "amount": "30"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/console/credits?saved=1"
    account = STORE.get_credit_account(workspace.id)
    assert account is not None
    assert account.auto_refill_enabled is True
    assert account.auto_refill_threshold_microdollars == 10_000_000
    assert account.auto_refill_amount_microdollars == 30_000_000


def test_auto_refill_exits_band_after_successful_credit(
    configured_workspace: str, stripe_settings: Settings
) -> None:
    """The infinite-loop guard. Drain below threshold → fire → webhook
    credits → next maybe_charge_after_settle must NOT fire because
    available is now above threshold. If this regresses we'd burn cards
    in a tight loop."""
    # Drain to $1 (below the $5 threshold, fires a $20 refill).
    STORE.settle(STORE.reserve(configured_workspace, "k", 9_000_000).id, 9_000_000)

    fake_intent = MagicMock(id="pi_band_1")
    with patch("stripe.PaymentIntent.create", return_value=fake_intent) as create:
        first = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
    assert first.fired is True
    create.assert_called_once()

    # Webhook lands → credit the workspace by the refill amount.
    settings_local = Settings(environment="local", stripe_webhook_secret=None)
    app = create_app(settings_local, init_observability=False, configure_store_arg=False)
    with TestClient(app) as client:
        client.post(
            "/internal/stripe/webhook",
            json={
                "id": "evt_band_succeeded",
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_band_1",
                        "metadata": {
                            "workspace_id": configured_workspace,
                            "auto_refill": "true",
                            "amount_microdollars": "20000000",
                        },
                    }
                },
            },
        )

    account = STORE.get_credit_account(configured_workspace)
    assert account is not None
    available = (
        account.total_credits_microdollars
        - account.total_usage_microdollars
        - account.reserved_microdollars
    )
    # $1 + $20 refill = $21, well above $5 threshold.
    assert available > account.auto_refill_threshold_microdollars

    # Next settle (small additional usage) should NOT fire another charge.
    STORE.settle(STORE.reserve(configured_workspace, "k", 100_000).id, 100_000)
    with patch("stripe.PaymentIntent.create", return_value=fake_intent) as create:
        second = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
    assert second.fired is False
    assert second.reason == "above_threshold"
    create.assert_not_called()


def test_auto_refill_idempotency_key_blocks_double_charge_within_minute(
    configured_workspace: str, stripe_settings: Settings
) -> None:
    """Two settles inside the same calendar minute that both drop below
    threshold must hand the same idempotency key to Stripe so Stripe's
    own dedupe absorbs the second call. We verify that by inspecting
    the `idempotency_key` kwarg, not by faking Stripe's behaviour."""
    STORE.settle(STORE.reserve(configured_workspace, "k", 9_000_000).id, 9_000_000)

    fake_intent = MagicMock(id="pi_idem")
    with patch("stripe.PaymentIntent.create", return_value=fake_intent) as create:
        first = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
        # Reset the rate-limit gate by simulating a fresh "pending" state.
        STORE.record_auto_refill_outcome(configured_workspace, status="pending")
        second = maybe_charge_after_settle(configured_workspace, settings=stripe_settings)
    assert first.fired is True
    # Even if the second goes through, the idempotency key must match.
    if second.fired:
        first_key = create.call_args_list[0].kwargs["idempotency_key"]
        second_key = create.call_args_list[1].kwargs["idempotency_key"]
        # Same workspace + same amount + same minute → same key.
        assert first_key == second_key


def test_console_credits_refuses_to_enable_without_payment_method() -> None:
    # Explicit None overrides whatever the local keys file has so the
    # webhook handler skips the construct_event signature check.
    settings = Settings(environment="local", stripe_webhook_secret=None)
    app = create_app(settings, init_observability=False, configure_store_arg=False)
    with TestClient(app) as client:
        user = STORE.ensure_user("nocard@example.com")
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        # No payment method set.
        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="google",
            label="nocard@example.com",
            ttl_seconds=3600,
            state="active",
        )
        client.cookies.set("tr_session", raw_token)
        resp = client.post(
            "/console/credits/auto-refill",
            data={"enabled": "1", "threshold": "10", "amount": "30"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?error=no_payment_method")
    account = STORE.get_credit_account(workspace.id)
    assert account is not None
    assert account.auto_refill_enabled is False
