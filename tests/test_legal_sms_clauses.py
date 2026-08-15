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
from trusted_router.dashboard import public_privacy_html, public_terms_html


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
