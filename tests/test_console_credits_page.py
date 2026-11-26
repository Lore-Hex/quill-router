"""The redesigned Credits page: presets, fee preview and the state switches."""

from __future__ import annotations

import html
import json
import re
from typing import Any

import pytest
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
    "card": {500: 120, 2000: 137, 2500: 159, 5000: 272, 10000: 495, 20000: 944},
    "ach": {500: 8, 2000: 26, 2500: 32, 5000: 62, 10000: 122, 20000: 243},
    "stablecoin": {500: 12, 2000: 47, 2500: 59, 5000: 116, 10000: 230, 20000: 458},
    "adyen": {500: 80, 2000: 80, 2500: 80, 5000: 80, 10000: 80, 20000: 80},
}


def _console_client(**settings: Any) -> tuple[TestClient, Any, Any]:
    app = create_app(Settings(environment="local", **settings), init_observability=False)
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


def _credit_timing_from_page(page: str) -> dict[str, Any]:
    match = re.search(r'data-credit-timing="([^"]+)"', page)
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
    # Established, so the $100 PayPal checkout is past the new-workspace cap.
    user.identity_status = "approved"
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
    assert 'max="9570.76" step="0.01"' in page
    assert schedule["maximum_cents"] == {
        "auto": 957076, "card": 957076, "ach": 999250,
        "stablecoin": 977666, "paypal": 1000000, "adyen": 1000000,
    }
    assert '<option value="ach">Bank account (ACH), up to 4 business days</option>' in page
    assert 'data-method-icon="bank" data-methods="ach"' in page
    # $25 on a card: $1.59 fee, $26.59 at checkout.
    assert ">$25.00</dd>" in page
    assert ">$1.59</dd>" in page
    assert ">$26.59</dd>" in page
    assert "Top up to keep prepaid routes flowing" not in page
    assert "Lifetime usage" not in page
    assert "Migration credits" not in page


def test_each_payment_method_names_its_time_to_credit_before_checkout() -> None:
    """Time to credit is on the picker, under it and in the summary, not only
    inside the collapsed Fees note after the button."""
    client, _, _ = _console_client()
    page = client.get("/console/credits").text

    for option in (
        '<option value="auto">Card, instant</option>',
        '<option value="ach">Bank account (ACH), up to 4 business days</option>',
        '<option value="stablecoin">USDC, usually within minutes</option>',
        '<option value="paypal">PayPal, usually instant</option>',
    ):
        assert option in page, option
    # The default method's line is server-rendered so it reads without script,
    # and the select points at it.
    assert (
        '<p class="credits-timing" id="credits-timing" aria-live="polite" '
        "data-credit-timing-line>"
    ) in page
    assert (
        "<span data-credit-timing-text>Credit is available as soon as your "
        "card payment goes through.</span>"
    ) in page
    assert 'aria-label="Payment method" aria-describedby="credits-timing"' in page
    assert "<dt>Credit available</dt>\n        <dd data-summary-timing>Instant</dd>" in page
    assert "credits-timing is-slow" not in page
    submit = page.index("data-checkout-submit")
    assert page.index("data-credit-timing-line") < submit
    assert page.index("data-summary-timing") < submit
    assert page.index("data-summary-timing") > page.index("data-summary-fee")
    # The Fees note keeps its settlement detail.
    fees = page[page.index('<details class="credits-fees">'):]
    assert "ACH can take up to four business days" in fees

    # console.js swaps the line and the summary from this table: one entry per
    # method the page can post, with only the bank path marked slower.
    timing = _credit_timing_from_page(page)
    assert set(timing) == set(CHECKOUT_METHOD_RAILS)
    assert {method for method, entry in timing.items() if entry.get("slow")} == {"ach"}
    assert timing["ach"] == {
        "label": "up to 4 business days",
        "summary": "Up to 4 business days",
        "line": (
            "Credit is added once the bank payment settles, which can take up to "
            "4 business days. Pay by card if you need it sooner."
        ),
        "slow": True,
    }
    assert timing["auto"] == timing["card"]
    assert timing["auto"]["summary"] == "Instant"
    assert timing["stablecoin"]["summary"] == "Usually within minutes"
    assert timing["paypal"]["summary"] == "Usually instant"
    assert timing["adyen"]["summary"] == "Usually within minutes"
    for method, entry in timing.items():
        assert entry["summary"] == entry["label"][:1].upper() + entry["label"][1:], method
        for text in (entry["label"], entry["summary"], entry["line"]):
            assert "!" not in text and "—" not in text, (method, text)


@pytest.mark.parametrize("amount", ["1e1000000", "NaN", "Infinity", "10000"])
def test_console_checkout_bad_amount_is_400(amount) -> None:
    client, _, _ = _console_client()
    response = client.post("/console/credits/checkout",
                           data={"amount": amount, "payment_method": "card"},
                           follow_redirects=False)
    assert response.status_code == 400, response.text
    if amount == "10000":
        assert "$9,570.76" in response.json()["error"]["message"]


def test_console_checkout_exact_maximum_succeeds() -> None:
    client, _, _ = _console_client()
    response = client.post("/console/credits/checkout",
                           data={"amount": "other", "other_amount": "9570.76",
                                 "payment_method": "card"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/console/credits?checkout=mock"


def test_checkout_presets_follow_the_verification_remainder() -> None:
    assert checkout_presets(None) == ([5, 25, 200], 25)
    assert checkout_presets(0) == ([5, 25, 200], 25)
    assert checkout_presets(25 * MICRODOLLARS_PER_DOLLAR) == ([5, 25, 200], 25)
    assert checkout_presets(12 * MICRODOLLARS_PER_DOLLAR) == ([5, 12, 200], 12)
    # Partial cents round up to the next whole dollar.
    assert checkout_presets(7 * MICRODOLLARS_PER_DOLLAR + 1) == ([5, 8, 200], 8)
    assert checkout_presets(5 * MICRODOLLARS_PER_DOLLAR) == ([5, 25, 200], 5)
    assert checkout_presets(1) == ([5, 25, 200], 5)
    # A remainder at or past the last preset keeps the row in order.
    assert checkout_presets(200 * MICRODOLLARS_PER_DOLLAR) == ([5, 25, 200], 200)
    assert checkout_presets(250 * MICRODOLLARS_PER_DOLLAR) == ([5, 25, 200], 200)


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
        assert response.status_code == 400, bad
        assert response.json()["error"]["message"], bad


def test_status_lines_render_for_redirect_codes_only() -> None:
    client, _, _ = _console_client()

    expected = {
        "invalid_checkout": "Enter a dollar amount with at most two decimals, within the maximum shown for your payment method.",
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
    # The saved card keeps its time to credit when /stripe-details relabels it.
    assert (
        '<option value="auto" data-credits-saved-card-label '
        'data-credits-label-suffix=", instant">Saved card, instant</option>'
    ) in page
    assert '<option value="card">Another card, instant</option>' in page
    assert "<dd data-summary-timing>Instant</dd>" in page
    assert 'action="/console/credits/payment-methods/manage"' in page
    assert 'action="/console/credits/payment-methods/remove"' in page
    assert 'action="/console/credits/payment-methods/add"' not in page
    assert 'role="switch" name="enabled" value="1"' in page
    assert "disabled data-auto-refill-switch" not in page
    assert "When balance is below" in page and "Refill with" in page
    assert "pm_mock_f00d1234" not in page
    assert "Test ····1234" in details
    assert "pm_mock_f00d1234" not in details


@pytest.mark.parametrize("method,maximum,custom_maximum", [
    ("card", "9,570.76", "9,176.83"),
    ("ach", "9,992.50", "9,985.00"),
    ("stablecoin", "9,776.66", "9,563.08"),
])
@pytest.mark.parametrize("multiplier", [15000, 30000])
def test_console_checkout_above_principal_ceiling_reports_rail_maximum(
    method, maximum, custom_maximum, multiplier,
):
    client, _, _ = _console_client(stripe_paypal_fee_multiplier_basis_points=multiplier)
    response = client.post("/console/credits/checkout",
                           data={"amount": "10000.01", "payment_method": method},
                           follow_redirects=False)
    assert response.status_code == 400, response.text
    maximum = maximum if multiplier == 15000 else custom_maximum
    assert f"${maximum}" in response.json()["error"]["message"]


@pytest.mark.parametrize("amount", ["25.0000001", "0.9999999", "3.3000000000000003"])
def test_console_checkout_submicro_precision_stays_accepted(amount):
    client, _, _ = _console_client()
    response = client.post("/console/credits/checkout",
                           data={"amount": amount, "payment_method": "card"},
                           follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/console/credits?checkout=mock"
