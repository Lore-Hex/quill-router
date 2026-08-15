"""The SMS clauses carriers actually check for.

A2P 10DLC campaign vetting fetches the brand's privacy policy and terms and
looks for specific language. Both TrustedRouter campaigns were REJECTED for
their absence — 30882 (terms) and 30908 (privacy) — so these are not
decorative: dropping a sentence here silently un-registers our ability to send
SMS, and the failure surfaces days later as a rejected campaign rather than as
a broken page.
"""

from __future__ import annotations

import re

import pytest

from trusted_router.config import Settings
from trusted_router.dashboard import (
    public_privacy_html,
    public_sms_html,
    public_terms_html,
)


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).lower()


@pytest.fixture
def privacy() -> str:
    return _text(public_privacy_html(Settings()))


@pytest.fixture
def terms() -> str:
    return _text(public_terms_html(Settings()))


class TestPrivacyPolicy:
    def test_states_that_mobile_data_is_not_shared(self, privacy: str) -> None:
        # The exact clause carrier vetting looks for. Error 30908 is raised
        # when it cannot be found.
        assert "no mobile information will be shared with third parties" in privacy
        assert "marketing or promotional purposes" in privacy

    def test_says_opt_in_data_is_never_sold(self, privacy: str) -> None:
        assert "never sold" in privacy or "not be sold" in privacy

    @pytest.mark.parametrize(
        "clause",
        ["reply stop", "reply help", "message frequency varies",
         "message and data rates may apply"],
    )
    def test_carries_the_standard_disclosures(self, privacy: str, clause: str) -> None:
        assert clause in privacy


class TestTerms:
    @pytest.mark.parametrize(
        "clause",
        ["reply stop", "reply help", "message frequency varies",
         "message and data rates may apply", "not liable for delayed"],
    )
    def test_carries_the_sms_program_terms(self, terms: str, clause: str) -> None:
        # Error 30882 is raised when the terms do not describe the program.
        assert clause in terms

    def test_consent_is_not_a_condition_of_purchase(self, terms: str) -> None:
        assert "not a condition of purchas" in terms

    def test_notifications_are_not_promised_as_reliable(self, terms: str) -> None:
        # We page people about outages over networks that drop messages; saying
        # so is both honest and what keeps this out of life-safety reliance.
        assert "not a guaranteed delivery channel" in terms


class TestSmsProgramPage:
    """Campaign vetting must VERIFY an opt-in it cannot reach.

    Ours happens in account settings, behind a sign-in, so a reviewer sees
    nothing at all. Publishing the exact consent language is the only way a
    web-form opt-in behind auth can be checked from outside.
    """

    @pytest.fixture
    def sms(self) -> str:
        return _text(public_sms_html(Settings()))

    def test_publishes_the_exact_consent_language(self, sms: str) -> None:
        assert "i agree to receive account alerts" in sms
        assert "reply stop to unsubscribe or help for help" in sms

    def test_names_both_sending_numbers(self, sms: str) -> None:
        # A recipient checking who texted them should find the number here.
        assert "505 531 3623" in sms
        assert "505 421 5808" in sms

    def test_states_the_checkbox_is_not_pre_ticked(self, sms: str) -> None:
        # Pre-checked consent is not consent, and vetting asks.
        assert "unticked by default" in sms

    @pytest.mark.parametrize(
        "clause",
        ["message and data rates may apply", "message frequency varies",
         "no mobile information will be shared with third parties",
         "not a condition of purchas"],
    )
    def test_repeats_the_required_disclosures(self, sms: str, clause: str) -> None:
        assert clause in sms


class TestDiscoverability:
    def test_the_legal_pages_link_to_the_programme_page(self) -> None:
        # A page nothing links to is a page a crawler does not find, which is
        # the failure mode this whole exercise is about.
        assert '/sms' in public_privacy_html(Settings())
        assert '/sms' in public_terms_html(Settings())
