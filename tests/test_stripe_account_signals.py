from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.auto_refill import maybe_charge_after_settle
from trusted_router.services.stripe_billing import create_checkout_session
from trusted_router.services.stripe_radar import account_signals
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import User, Workspace
from trusted_router.typed_balance import live_credit_summary

_NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=UTC)
_SIGNAL_KEYS = {
    "tr_workspace_age_minutes",
    "tr_user_age_minutes",
    "tr_email_verified",
    "tr_phone_verified",
    "tr_identity_verified",
    "tr_owned_workspaces",
    "tr_prior_topup_cents",
}


def test_account_signals_are_strings_in_whole_minutes() -> None:
    workspace = Workspace(
        id="ws-1", name="w", owner_user_id="u-1", created_at="2026-10-14T15:54:30Z"
    )
    user = User(
        id="u-1",
        email="payer@example.com",
        created_at="2026-10-11T18:00:00.000001Z",
        email_verified=True,
        phone_verified=False,
        identity_status="approved",
        owner_workspace_count=2,
    )

    signals = account_signals(
        workspace=workspace, user=user, prior_topup_microdollars=25_000_000, now=_NOW
    )

    assert signals == {
        "tr_workspace_age_minutes": "125",
        "tr_user_age_minutes": "4319",
        "tr_email_verified": "true",
        "tr_phone_verified": "false",
        "tr_identity_verified": "true",
        "tr_owned_workspaces": "2",
        "tr_prior_topup_cents": "2500",
    }


def test_account_signals_leave_out_what_was_not_read() -> None:
    unreadable = Workspace(id="ws-1", name="w", owner_user_id="u-1", created_at="yesterday")
    assert account_signals(
        workspace=unreadable, user=None, prior_topup_microdollars=None, now=_NOW
    ) == {}

    future = Workspace(
        id="ws-2", name="w", owner_user_id="u-1", created_at="2026-10-14T18:05:00Z"
    )
    assert account_signals(
        workspace=future, user=None, prior_topup_microdollars=0, now=_NOW
    ) == {"tr_workspace_age_minutes": "0", "tr_prior_topup_cents": "0"}


def _checkout(
    monkeypatch: pytest.MonkeyPatch, user_headers: dict[str, str]
) -> dict[str, Any]:
    app = create_app(
        Settings(environment="test", stripe_secret_key="sk_test_signals"),  # noqa: S106
        init_observability=False,
    )
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_signals", "url": "https://checkout.stripe.test/signals"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )
    with TestClient(app) as local_client:
        response = local_client.post(
            "/v1/billing/checkout",
            headers=user_headers,
            json={"amount": 25, "payment_method": "card"},
        )
    assert response.status_code == 201, response.text
    return captured


def test_card_checkout_sends_account_signals_to_stripe(
    monkeypatch: pytest.MonkeyPatch, user_headers: dict[str, str]
) -> None:
    captured = _checkout(monkeypatch, user_headers)

    metadata = captured["payment_intent_data"]["metadata"]
    assert _SIGNAL_KEYS <= set(metadata)
    assert captured["metadata"] == metadata
    user = STORE.get_user(metadata["initiating_user_id"])
    assert user is not None
    assert metadata["tr_email_verified"] == ("true" if user.email_verified else "false")
    assert metadata["tr_owned_workspaces"] == str(user.owner_workspace_count)
    assert metadata["tr_prior_topup_cents"] == "0"
    assert int(metadata["tr_workspace_age_minutes"]) <= 1
    assert int(metadata["tr_user_age_minutes"]) <= 1
    assert metadata["credit_amount_microdollars"] == "25000000"


def test_checkout_still_goes_through_when_a_signal_read_fails(
    monkeypatch: pytest.MonkeyPatch, user_headers: dict[str, str]
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("spanner unavailable")

    monkeypatch.setattr(InMemoryStore, "get_lifetime_topup_microdollars", unavailable)

    captured = _checkout(monkeypatch, user_headers)

    metadata = captured["payment_intent_data"]["metadata"]
    assert "tr_prior_topup_cents" not in metadata
    assert "tr_user_age_minutes" in metadata
    assert "tr_workspace_age_minutes" in metadata


def test_checkout_describes_the_payer_not_the_workspace_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = STORE.ensure_user("owner-signals@example.com", email_verified=False)
    payer = STORE.ensure_user("payer-signals@example.com", email_verified=True)
    workspace = STORE.list_workspaces_for_user(owner.id)[0]
    assert STORE.add_lifetime_topup(payer.id, 25_000_000, "evt_prior_topup")
    captured: dict[str, Any] = {}

    def create_session(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"id": "cs_payer", "url": "https://checkout.stripe.test/payer"}

    monkeypatch.setattr(
        "trusted_router.services.stripe_billing.stripe.checkout.Session.create",
        create_session,
    )
    create_checkout_session(
        body=CheckoutRequest(amount=25, payment_method="card"),
        workspace_id=workspace.id,
        initiating_user_id=payer.id,
        customer_email=None,
        customer_id=None,
        settings=Settings(environment="test", stripe_secret_key="sk_test_payer"),  # noqa: S106
    )

    metadata = captured["payment_intent_data"]["metadata"]
    assert metadata["initiating_user_id"] == payer.id
    assert metadata["tr_email_verified"] == "true"
    assert metadata["tr_prior_topup_cents"] == "2500"


def test_auto_refill_sends_no_account_signals() -> None:
    """Auto-refill retries reuse one idempotency key, and Stripe refuses a
    retry whose parameters changed; ages change every minute."""
    settings = Settings(environment="test", stripe_secret_key="sk_test_dummy")  # noqa: S106
    user = STORE.ensure_user("refill-signals@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.update_auto_refill_settings(
        workspace.id,
        enabled=True,
        threshold_microdollars=5_000_000,
        amount_microdollars=20_000_000,
    )
    STORE.set_stripe_customer(
        workspace.id, customer_id="cus_test_123", payment_method_id="pm_test_456"
    )
    summary = live_credit_summary(workspace.id)
    assert summary is not None
    drain = summary["available"] - 1_000_000
    reservation = STORE.reserve(workspace.id, "fake-key-hash", drain)
    STORE.settle(reservation.id, drain)

    with patch("stripe.PaymentIntent.create", return_value=MagicMock(id="pi_signals")) as create:
        outcome = maybe_charge_after_settle(workspace.id, settings=settings)

    assert outcome.fired is True, outcome
    metadata = create.call_args.kwargs["metadata"]
    assert metadata["auto_refill"] == "true"
    assert not [key for key in metadata if key.startswith("tr_")]
