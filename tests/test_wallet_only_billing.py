from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, User, Workspace


@contextmanager
def _wallet_client(
    *,
    email: str | None = None,
    email_verified: bool = False,
) -> Iterator[tuple[TestClient, User, Workspace, str]]:
    app = create_app(Settings(environment="local"), init_observability=False)
    with TestClient(app) as client:
        user = STORE.create_wallet_user("0x1111111111111111111111111111111111111111")
        if email:
            updated = STORE.set_user_email(user.id, email)
            assert updated is not None
            user = updated
        if email_verified:
            verified = STORE.mark_user_email_verified(user.id)
            assert verified is not None
            user = verified
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        raw_session, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="metamask",
            label=user.wallet_address or "wallet",
            workspace_id=workspace.id,
            ttl_seconds=3600,
            state="active",
        )
        client.cookies.set("tr_session", raw_session)
        yield client, user, workspace, raw_session


@pytest.mark.parametrize(
    "payment_method",
    ["auto", "card", "ach", "bank", "us_bank_account", "paypal", "adyen"],
)
def test_wallet_only_api_checkout_rejects_every_non_stablecoin_rail(
    payment_method: str,
) -> None:
    with _wallet_client() as (client, _user, _workspace, _session):
        response = client.post(
            "/v1/billing/checkout",
            json={"amount": 25, "payment_method": payment_method},
        )

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["message"] == (
        "Wallet-only accounts can only fund with stablecoin. "
        "Add and verify an email to use other payment methods."
    )
    assert error["type"] == "forbidden"
    assert error["code"] == 403
    assert error["source"] == "router"


@pytest.mark.parametrize("payment_method", ["stablecoin", "crypto", "usdc"])
def test_wallet_only_api_checkout_allows_stablecoin_aliases(
    payment_method: str,
) -> None:
    with _wallet_client() as (client, _user, _workspace, _session):
        response = client.post(
            "/v1/billing/checkout",
            json={"amount": 25, "payment_method": payment_method},
        )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["mode"] == "mock_stablecoin"


@pytest.mark.parametrize(
    ("path", "json_body"),
    [
        ("/v1/billing/payment-methods/setup", None),
        ("/v1/billing/portal", {}),
        ("/v1/billing/paypal/orders/order-test/capture", None),
    ],
)
def test_wallet_only_api_rejects_direct_traditional_billing_routes(
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    with _wallet_client() as (client, _user, _workspace, _session):
        response = client.post(path, json=json_body)

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "forbidden"


def test_wallet_only_management_key_inherits_creator_billing_restriction() -> None:
    with _wallet_client() as (client, user, workspace, _session):
        raw_key, _ = STORE.create_api_key(
            workspace_id=workspace.id,
            name="wallet-management",
            creator_user_id=user.id,
            management=True,
        )
        response = client.post(
            "/v1/billing/checkout",
            headers={"authorization": f"Bearer {raw_key}"},
            json={"amount": 25, "payment_method": "card"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "forbidden"


def test_wallet_only_console_offers_stablecoin_without_card_controls() -> None:
    with _wallet_client() as (client, _user, _workspace, _session):
        response = client.get("/console/credits")

    assert response.status_code == 200
    assert 'name="payment_method" value="stablecoin"' in response.text
    assert 'value="Stablecoin (USDC)"' in response.text
    assert '<option value="auto">' not in response.text
    assert '<option value="ach">' not in response.text
    assert '<option value="paypal">' not in response.text
    assert '<option value="adyen">' not in response.text
    assert 'action="/console/credits/payment-methods/add"' not in response.text
    assert 'action="/console/credits/payment-methods/manage"' not in response.text
    assert 'action="/console/credits/auto-refill"' not in response.text
    assert "Wallet-only accounts can fund with stablecoin" in response.text


def test_wallet_only_console_rejects_direct_card_and_auto_refill_posts() -> None:
    with _wallet_client() as (client, _user, workspace, _session):
        checkout = client.post(
            "/console/credits/checkout",
            data={"amount": "25", "payment_method": "card"},
            follow_redirects=False,
        )
        add_method = client.post(
            "/console/credits/payment-methods/add",
            follow_redirects=False,
        )
        manage_method = client.post(
            "/console/credits/payment-methods/manage",
            follow_redirects=False,
        )
        paypal_capture = client.get(
            "/console/credits/paypal/capture?token=order-test",
            follow_redirects=False,
        )
        auto_refill = client.post(
            "/console/credits/auto-refill",
            data={"enabled": "1", "threshold": "10", "amount": "25"},
            follow_redirects=False,
        )
        account = STORE.get_credit_account(workspace.id)

    for response in (
        checkout,
        add_method,
        manage_method,
        paypal_capture,
        auto_refill,
    ):
        assert response.status_code == 303
        assert response.headers["location"] == ("/console/credits?error=stablecoin_only")
    assert account is not None
    assert account.stripe_customer_id is None
    assert account.stripe_payment_method_id is None
    assert account.auto_refill_enabled is False


def test_wallet_only_console_stablecoin_checkout_still_works() -> None:
    with _wallet_client() as (client, _user, _workspace, _session):
        response = client.post(
            "/console/credits/checkout",
            data={"amount": "25", "payment_method": "stablecoin"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/console/credits?checkout=mock"


def test_wallet_only_console_can_remove_a_legacy_saved_card() -> None:
    with _wallet_client() as (client, _user, workspace, _session):
        STORE.set_stripe_customer(
            workspace.id,
            customer_id="cus_wallet_legacy",
            payment_method_id="pm_wallet_legacy",
        )
        page = client.get("/console/credits")
        response = client.post(
            "/console/credits/payment-methods/remove",
            follow_redirects=False,
        )
        account = STORE.get_credit_account(workspace.id)

    assert 'action="/console/credits/payment-methods/remove"' in page.text
    assert 'action="/console/credits/payment-methods/add"' not in page.text
    assert response.status_code == 303
    assert response.headers["location"] == ("/console/credits?payment_method=removed")
    assert account is not None
    assert account.stripe_payment_method_id is None
    assert account.auto_refill_enabled is False


def test_unverified_wallet_email_does_not_unlock_card_billing() -> None:
    with _wallet_client(email="wallet@example.com") as (
        client,
        _user,
        _workspace,
        _session,
    ):
        response = client.post(
            "/v1/billing/checkout",
            json={"amount": 25, "payment_method": "card"},
        )

    assert response.status_code == 403


def test_verified_wallet_email_unlocks_standard_billing_options() -> None:
    with _wallet_client(
        email="wallet@example.com",
        email_verified=True,
    ) as (client, _user, _workspace, _session):
        checkout = client.post(
            "/v1/billing/checkout",
            json={"amount": 25, "payment_method": "card"},
        )
        page = client.get("/console/credits")

    assert checkout.status_code == 201, checkout.text
    assert '<option value="auto">Stripe card</option>' in page.text
    assert 'action="/console/credits/payment-methods/add"' in page.text
    assert 'action="/console/credits/auto-refill"' in page.text
