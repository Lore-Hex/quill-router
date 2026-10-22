"""The redesigned Credits page: presets, fee preview and the state switches."""

from __future__ import annotations

import html
import json
import re
from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
)
from trusted_router.routes.console.credits import (
    CHECKOUT_DEFAULT_PRESET_DOLLARS,
    CHECKOUT_METHOD_RAILS,
    CHECKOUT_PRESET_DOLLARS,
    checkout_fee_schedule,
    checkout_presets,
)
from trusted_router.schemas import CheckoutRequest
from trusted_router.services.checkout_fees import (
    FEE_RAILS,
    FeeRail,
    rail_fee_rates,
    rail_processing_fee,
)
from trusted_router.services.paypal_billing import create_paypal_checkout_session
from trusted_router.services.stripe_billing import create_checkout_session
from trusted_router.storage import STORE

# Fees the design was signed off with (config defaults: card 2.9% + 30c with
# an 80c floor, ACH 0.8% capped at $5, USDC 1.5%, all x1.5; Adyen 0% + 0c with
# the 80c floor and no surcharge).
EXPECTED_FEE_CENTS: dict[FeeRail, dict[int, int]] = {
    "card": {500: 120, 2000: 137, 2500: 159, 5000: 272, 10000: 495},
    "ach": {500: 8, 2000: 26, 2500: 32, 5000: 62, 10000: 122},
    "stablecoin": {500: 12, 2000: 47, 2500: 59, 5000: 116, 10000: 230},
    "adyen": {500: 80, 2000: 80, 2500: 80, 5000: 80, 10000: 80},
}


def _console_client() -> tuple[TestClient, Any, Any]:
    app = create_app(Settings(environment="local"), init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("credits-page@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="email",
        label="credits-page@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, user, workspace


def _fee_schedule_from_page(page: str) -> dict[str, Any]:
    match = re.search(r'data-fee-schedule="([^"]+)"', page)
    assert match is not None
    return json.loads(html.unescape(match.group(1)))


def test_fee_table_matches_processing_fee_for_every_preset_and_rail() -> None:
    settings = Settings(environment="local")
    for rail, expected in EXPECTED_FEE_CENTS.items():
        rates = rail_fee_rates(settings, rail)
        for credit_cents, fee_cents in expected.items():
            assert rates.processing_fee(credit_cents).processing_fee_cents == fee_cents, (
                rail,
                credit_cents,
            )
            assert rail_processing_fee(settings, rail, credit_cents).processing_fee_cents == fee_cents
    assert set(FEE_RAILS) == set(EXPECTED_FEE_CENTS)
    assert set(CHECKOUT_METHOD_RAILS.values()) <= set(FEE_RAILS)


def test_checkout_services_charge_the_fee_the_page_previews() -> None:
    """The Stripe and PayPal checkouts (mock mode: no keys) price from the
    same rates the page embeds, so the summary equals the line items."""
    settings = Settings(environment="local")
    _, user, workspace = _console_client()
    for dollars in (5, 20, 100):
        for method in ("auto", "ach", "stablecoin"):
            data = create_checkout_session(
                body=CheckoutRequest(amount=dollars, payment_method=method),
                workspace_id=workspace.id,
                initiating_user_id=user.id,
                customer_email=None,
                customer_id=None,
                settings=settings,
            )
            expected = rail_processing_fee(
                settings, CHECKOUT_METHOD_RAILS[method], dollars * 100
            )
            assert data["processing_fee_microdollars"] == expected.processing_fee_microdollars, (
                method,
                dollars,
            )
        paypal = create_paypal_checkout_session(
            body=CheckoutRequest(amount=max(dollars, 20), payment_method="paypal"),
            workspace_id=workspace.id,
            initiating_user_id=user.id,
            customer_email=None,
            settings=settings,
        )
        expected = rail_processing_fee(settings, "card", max(dollars, 20) * 100)
        assert paypal["processing_fee_microdollars"] == expected.processing_fee_microdollars


def test_page_embeds_the_fee_schedule_and_the_default_summary() -> None:
    client, _, _ = _console_client()
    page = client.get("/console/credits").text

    schedule = _fee_schedule_from_page(page)
    assert schedule == checkout_fee_schedule(Settings(environment="local"))
    assert schedule["rails"] == CHECKOUT_METHOD_RAILS
    assert set(schedule["rates"]) == set(FEE_RAILS)
    assert 'data-paypal-minimum="20"' in page
    for preset in CHECKOUT_PRESET_DOLLARS:
        assert f'name="amount" value="{preset}"' in page
    assert f'value="{CHECKOUT_DEFAULT_PRESET_DOLLARS}" checked' in page
    assert 'name="amount" value="other"' in page
    assert 'name="other_amount"' in page
    assert '<option value="ach">Bank account (ACH)</option>' in page
    assert 'data-method-icon="bank" data-methods="ach"' in page
    # $20 on a card: $1.37 fee, $21.37 at checkout.
    assert ">$20.00</dd>" in page
    assert ">$1.37</dd>" in page
    assert ">$21.37</dd>" in page
    assert "Top up to keep prepaid routes flowing" not in page
    assert "Lifetime usage" not in page
    assert "Migration credits" not in page


def test_checkout_presets_follow_the_verification_remainder() -> None:
    assert checkout_presets(None) == ([5, 20, 100], 20)
    assert checkout_presets(0) == ([5, 20, 100], 20)
    assert checkout_presets(25 * MICRODOLLARS_PER_DOLLAR) == ([5, 25, 100], 25)
    # Partial cents round up to the next whole dollar.
    assert checkout_presets(7 * MICRODOLLARS_PER_DOLLAR + 1) == ([5, 8, 100], 8)
    assert checkout_presets(5 * MICRODOLLARS_PER_DOLLAR) == ([5, 20, 100], 5)
    assert checkout_presets(1) == ([5, 20, 100], 5)
    # A remainder at or past the last preset keeps the row in order.
    assert checkout_presets(100 * MICRODOLLARS_PER_DOLLAR) == ([5, 20, 100], 100)
    assert checkout_presets(150 * MICRODOLLARS_PER_DOLLAR) == ([5, 20, 100], 100)


def test_verification_entry_preselects_the_remaining_amount() -> None:
    client, _, _ = _console_client()
    page = client.get("/console/credits?purpose=identity_verification").text

    assert "Verification needs $25.00 more in top-ups." in page
    assert 'name="amount" value="25" checked' in page
    assert 'name="purpose" value="identity_verification"' in page
    assert ">$25.00</dd>" in page
    assert ">$1.59</dd>" in page
    assert VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS == 25 * MICRODOLLARS_PER_DOLLAR

    plain = client.get("/console/credits").text
    assert "Verification needs" not in plain
    assert 'name="purpose"' not in plain


def test_other_amount_posts_through_the_existing_checkout() -> None:
    client, _, _ = _console_client()

    other = client.post(
        "/console/credits/checkout",
        data={"amount": "other", "other_amount": "50", "payment_method": "auto"},
        follow_redirects=False,
    )
    assert other.status_code == 303
    assert other.headers["location"] == "/console/credits?checkout=mock"

    for bad in ("", "0", "abc", "10001"):
        response = client.post(
            "/console/credits/checkout",
            data={"amount": "other", "other_amount": bad, "payment_method": "auto"},
            follow_redirects=False,
        )
        assert response.status_code == 303, bad
        assert response.headers["location"] == "/console/credits?error=invalid_checkout", bad


def test_status_lines_render_for_redirect_codes_only() -> None:
    client, _, _ = _console_client()

    expected = {
        "invalid_checkout": "Enter a whole-dollar amount from $1 to $10,000; PayPal starts at $20.",
        "stablecoin_only": "This account adds credits with USDC only.",
        "checkout_unavailable": "Checkout is temporarily unavailable.",
        "paypal_capture_failed": "The PayPal payment failed to complete.",
        "paypal_missing_order": "PayPal sent you back without an order to confirm.",
    }
    for code, copy in expected.items():
        assert copy in client.get(f"/console/credits?error={code}").text, code
    unknown = client.get("/console/credits?error=something_else").text
    plain = client.get("/console/credits").text
    # Only the PayPal line's empty slot is on a plain visit, and it is hidden.
    for page in (plain, unknown):
        assert page.count('class="credits-status"') == 1
        assert 'role="status" data-paypal-minimum-message hidden' in page


def test_saved_card_state_offers_change_manage_remove_and_the_switch() -> None:
    client, _, workspace = _console_client()
    STORE.set_stripe_customer(
        workspace.id, customer_id="cus_saved", payment_method_id="pm_mock_f00d1234"
    )

    page = client.get("/console/credits").text
    details = client.get("/console/credits/stripe-details").text

    assert page.count("Saved card") >= 3  # chip, select option, row title, refill block
    assert "data-checkout-change" in page
    assert '<option value="auto" data-credits-saved-card-label>' in page
    assert '<option value="card">Another card</option>' in page
    assert 'action="/console/credits/payment-methods/manage"' in page
    assert 'action="/console/credits/payment-methods/remove"' in page
    assert 'action="/console/credits/payment-methods/add"' not in page
    assert 'role="switch" name="enabled" value="1"' in page
    assert "disabled data-auto-refill-switch" not in page
    assert "When balance is below" in page and "Refill with" in page
    assert "pm_mock_f00d1234" not in page
    assert "Test ····1234" in details
    assert "pm_mock_f00d1234" not in details
