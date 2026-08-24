"""The public /sms page must show the REAL enrolment form, not a description of it.

Campaign 30909 was rejected because the submission and /sms both described a
consent checkbox the console did not have. The sentence is now shared, but the
fields around it were not, so the public page could still show a form that did
not match the live one. These tests hold the two together.
"""

from __future__ import annotations

import re

import pytest

from trusted_router.config import Settings
from trusted_router.dashboard import public_sms_html


@pytest.fixture
def sms_html() -> str:
    return public_sms_html(Settings())


class TestThePublicPageShowsTheForm:
    def test_it_renders_an_actual_consent_checkbox(self, sms_html: str) -> None:
        # Not prose about a checkbox — the input itself, which is what a vetter
        # who cannot sign in has to look at.
        assert re.search(r'<input[^>]*type="checkbox"[^>]*name="sms_consent"', sms_html)

    def test_the_preview_checkbox_is_not_pre_ticked(self, sms_html: str) -> None:
        # Its default state is the claim being made to the carrier.
        block = _consent_input(sms_html)
        assert "checked" not in block, f"preview checkbox is pre-ticked: {block}"

    def test_the_preview_cannot_collect_anything(self, sms_html: str) -> None:
        # Evidence, not a second enrolment path: every input is disabled, and the
        # form posts nowhere.
        assert "disabled" in _consent_input(sms_html)
        phone = re.search(r'<input[^>]*name="phone"[^>]*>', sms_html)
        assert phone and "disabled" in phone.group(0)
        assert not re.search(r'<form[^>]*action="[^"]*/console/', sms_html), (
            "the public preview points at the real POST endpoint"
        )

    def test_it_shows_the_number_field_and_the_submit_button(self, sms_html: str) -> None:
        assert 'name="phone"' in sms_html
        assert "Send code" in sms_html


class TestItCannotDriftFromTheConsole:
    def test_both_render_the_same_partial(self) -> None:
        # The guarantee is structural: one template, included twice. If someone
        # inlines either copy, this fails and the drift is caught here rather
        # than by a rejected campaign.
        from pathlib import Path

        root = Path("src/trusted_router/templates")
        console = (root / "console" / "settings.html").read_text()
        public = (root / "public" / "sms.html").read_text()

        assert "_sms_enrolment_fields.html" in console
        assert "_sms_enrolment_fields.html" in public
        # The console must not carry its own copy of the fields any more.
        assert 'name="sms_consent"' not in console, (
            "the console re-inlined the consent checkbox; it can now drift from /sms"
        )

    def test_the_shared_partial_still_carries_the_full_cta(self) -> None:
        from pathlib import Path

        fields = Path("src/trusted_router/templates/_sms_enrolment_fields.html").read_text()
        assert "_sms_consent.html" in fields, "the CTA sentence stopped being shared"
        assert 'name="sms_consent"' in fields
        assert "required" in fields, "the live form must still require consent"


def _consent_input(html: str) -> str:
    match = re.search(r'<input[^>]*name="sms_consent"[^>]*>', html)
    assert match, "no sms_consent input rendered on the public page"
    return match.group(0)
