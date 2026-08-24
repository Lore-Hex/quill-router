"""The notify docs page, checked against the code it describes.

Docs drift silently: nothing breaks when a price changes and the page still
quotes the old one, and the first person to notice is a customer who was billed
differently from what we published. So the numbers and names on the page are
asserted against the same constants the API uses.
"""

from __future__ import annotations

import re

import pytest

from trusted_router.config import Settings
from trusted_router.dashboard import public_page_html
from trusted_router.services.notify import CHANNELS, RefusalReason, price_for


@pytest.fixture
def page() -> str:
    return public_page_html(Settings(), "docs/notify")


@pytest.fixture
def text(page: str) -> str:
    return re.sub(r"<[^>]+>", " ", page).lower()


class TestPricingMatchesTheCode:
    def test_the_quoted_price_is_the_configured_price(self, text: str) -> None:
        # A page quoting a stale price is a billing dispute waiting to happen.
        dollars = price_for("sms", Settings()) / 1_000_000
        assert f"${dollars:.2f}" in text

    def test_push_is_documented_free_because_it_is_free(self, text: str) -> None:
        assert price_for("push", Settings()) == 0
        assert "free" in text

    def test_email_is_not_advertised_cheaper_than_sms(self) -> None:
        # Pricing email lower would make it the obvious channel to abuse; the
        # page must not imply otherwise.
        settings = Settings()
        assert price_for("email", settings) == price_for("sms", settings)


class TestItDescribesWhatExists:
    @pytest.mark.parametrize("channel", CHANNELS)
    def test_every_channel_is_documented(self, text: str, channel: str) -> None:
        assert channel in text

    @pytest.mark.parametrize(
        "refusal", ["phone_not_verified", "email_not_verified", "no_push_device"]
    )
    def test_named_refusals_are_real_refusal_reasons(self, text: str, refusal: str) -> None:
        # Naming a refusal the API cannot return sends people chasing a code
        # that will never appear in their logs.
        assert refusal in RefusalReason.__args__  # type: ignore[attr-defined]
        assert refusal in text

    def test_the_verification_limits_match_the_implementation(self, text: str) -> None:
        from trusted_router.phone_verification import (
            CODE_TTL_SECONDS,
            MAX_ATTEMPTS,
            RESEND_FLOOR_SECONDS,
        )

        assert f"{CODE_TTL_SECONDS // 60} minutes" in text
        assert str(MAX_ATTEMPTS) in text
        assert RESEND_FLOOR_SECONDS == 60 and "once a minute" in text


class TestItStatesTheThingsThatProtectUs:
    def test_it_says_the_destination_cannot_be_chosen(self, page: str, text: str) -> None:
        # The entire safety argument for exposing this on an inference key.
        # Checked against the raw HTML because the phrase spans a <code> tag.
        assert "There is no <code>to</code> parameter" in page
        assert "cannot be pointed at a stranger" in text

    def test_it_says_only_delivered_notifications_are_billed(self, text: str) -> None:
        assert "charged only when a notification is delivered" in text

    def test_it_does_not_promise_guaranteed_delivery(self, text: str) -> None:
        # We page people about outages over networks that drop messages. A doc
        # that promises delivery invites reliance we cannot honour.
        assert "not a guaranteed delivery channel" in text
