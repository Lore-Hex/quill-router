"""The settings page that actually lets someone add a phone number.

The API existed for a day before this did, which made the feature unreachable
for anyone who was not willing to hand-craft a POST — including its owner, who
went looking for it in the console and found nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import notify as notify_module
from trusted_router.services.telephony import TelephonyResult
from trusted_router.storage import STORE


class _Telephony:
    def __init__(self, *, delivered: bool = True) -> None:
        self.enabled = True
        self._delivered = delivered
        self.sent: list[tuple[str, str, str]] = []

    def send(self, channel, to, body, preferred_carrier=None):
        self.sent.append((channel, to, body))
        if self._delivered:
            return TelephonyResult(True, "telnyx", "queued")
        return TelephonyResult(False, None, "telnyx=500; twilio=500")


def _signed_in_client(settings: Settings) -> TestClient:
    """A signed-in console session on an app with the requested capability.

    /console/* rejects API-key Bearer auth and wants the cookie sign-in mints;
    each test creates only the one app whose SMS capability it needs.
    """
    client = TestClient(create_app(settings, init_observability=False))
    user = STORE.ensure_user("console-phone@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="t",
        ttl_seconds=3600,
        workspace_id=workspace.id,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)
    client.follow_redirects = False
    return client


@pytest.fixture
def client(test_settings: Settings) -> TestClient:
    return _signed_in_client(test_settings)


@pytest.fixture
def sms_client(test_settings: Settings) -> TestClient:
    return _signed_in_client(test_settings.model_copy(update={"notify_sms_available": True}))


@pytest.fixture
def carrier(monkeypatch) -> _Telephony:
    fake = _Telephony()
    monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)
    return fake


def _user():
    return STORE.find_user_by_email("console-phone@example.com")


class TestThePageOffersIt:
    def test_the_settings_page_invites_a_number(self, client) -> None:
        page = client.get("/console/settings")

        assert page.status_code == 200
        assert "Mobile number" in page.text
        assert "/console/settings/phone/start" in page.text

    def test_it_offers_a_phone_call(self, client) -> None:
        # A call needs no carrier registration and reaches landlines, so it must
        # be offered rather than hidden behind SMS failing first.
        page = client.get("/console/settings")

        assert "Phone call" in page.text

    def test_sms_is_hidden_until_carrier_registration_completes(self, client) -> None:
        page = client.get("/console/settings")

        assert '<option value="sms">' not in page.text
        assert "Text messages are coming once carrier registration completes." in page.text

    def test_sms_is_shown_when_the_capability_is_enabled(self, sms_client) -> None:
        page = sms_client.get("/console/settings")

        assert '<option value="sms">Text message</option>' in page.text


class TestVerifying:
    def test_a_code_can_be_requested_and_confirmed(self, client, carrier) -> None:
        started = client.post(
            "/console/settings/phone/start",
            data={"phone": "+1 (305) 951-1381", "channel": "voice", "sms_consent": "yes"},
        )
        assert started.status_code == 303, started.text
        assert carrier.sent, "no code was sent"

        _channel, _to, spoken = carrier.sent[0]
        code = "".join(ch for ch in spoken.split("is")[1] if ch.isdigit())[:6]

        confirmed = client.post("/console/settings/phone/confirm", data={"code": code})
        assert confirmed.status_code == 303

        user = _user()
        assert user.phone_verified
        assert user.phone == "+13059511381"

    def test_the_page_then_shows_the_verified_number(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )
        _c, _t, spoken = carrier.sent[0]
        code = "".join(ch for ch in spoken.split("is")[1] if ch.isdigit())[:6]
        client.post("/console/settings/phone/confirm", data={"code": code})

        page = client.get("/console/settings")

        assert "+13059511381" in page.text
        assert "verified" in page.text

    def test_a_bad_number_never_reaches_a_carrier(self, client, carrier) -> None:
        response = client.post(
            "/console/settings/phone/start",
            data={"phone": "3059511381", "channel": "voice", "sms_consent": "yes"},
        )

        assert response.status_code == 303
        assert "error=phone" in response.headers["location"]
        assert carrier.sent == []

    def test_a_wrong_code_does_not_verify(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )

        response = client.post("/console/settings/phone/confirm", data={"code": "000000"})

        assert "error=mismatch" in response.headers["location"]
        assert not _user().phone_verified

    def test_an_immediate_resend_is_refused(self, client, carrier) -> None:
        # Otherwise this form rings a stranger's phone as fast as it can be
        # submitted.
        body = {"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"}
        client.post("/console/settings/phone/start", data=body)

        second = client.post("/console/settings/phone/start", data=body)

        assert "error=rate" in second.headers["location"]
        assert len(carrier.sent) == 1

    def test_cancelling_and_starting_again_cannot_dodge_the_floor(self, client, carrier) -> None:
        # "Use a different number" must not be a way around the resend floor:
        # cancel, start, cancel, start would otherwise ring any number as fast
        # as the two forms can be submitted.
        body = {"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"}
        client.post("/console/settings/phone/start", data=body)
        client.post("/console/settings/phone/cancel")

        again = client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511382", "channel": "voice", "sms_consent": "yes"},
        )

        assert "error=rate" in again.headers["location"]
        assert len(carrier.sent) == 1
        # And the entry form tells the visitor how long, instead of a bare error.
        page = client.get("/console/settings")
        assert "You can request a code in" in page.text

    def test_sms_is_defensively_delivered_as_voice_while_unavailable(self, client, carrier) -> None:
        response = client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "sms", "sms_consent": "yes"},
        )

        assert response.headers["location"].endswith("sent=voice")
        assert carrier.sent[0][0] == "voice"
        assert _user().phone_code_channel == "voice"

        page = client.get(response.headers["location"])
        assert "We're calling +13059511381 now" in page.text

    def test_pending_voice_renders_call_again_after_the_floor(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )
        _user().phone_code_sent_at = "2000-01-01T00:00:00Z"

        page = client.get("/console/settings")

        assert "Call again" in page.text
        assert "Send as text instead" not in page.text
        assert "Use a different number" in page.text

    def test_pending_sms_renders_send_again_and_voice_alternative(
        self, sms_client, carrier
    ) -> None:
        response = sms_client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "sms", "sms_consent": "yes"},
        )
        assert _user().phone_code_channel == "sms"
        sent_page = sms_client.get(response.headers["location"])
        assert "Code texted to +13059511381" in sent_page.text
        _user().phone_code_sent_at = "2000-01-01T00:00:00Z"

        page = sms_client.get("/console/settings")

        assert "Send again" in page.text
        assert "Call me instead" in page.text

    def test_pending_page_disables_resend_and_shows_the_wait(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )

        page = client.get("/console/settings")

        assert "disabled>You can request another in " in page.text


class TestPostRedirectGet:
    def test_starting_redirects_rather_than_rendering(self, client, carrier) -> None:
        # A refresh on a rendered POST would ring the phone again.
        response = client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/console/settings")


class TestRemoval:
    def test_a_number_can_be_removed(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )
        _c, _t, spoken = carrier.sent[0]
        code = "".join(ch for ch in spoken.split("is")[1] if ch.isdigit())[:6]
        client.post("/console/settings/phone/confirm", data={"code": code})

        client.post("/console/settings/phone/remove")

        user = _user()
        assert not user.phone_verified
        assert user.phone is None


class TestCancellation:
    def test_cancel_clears_pending_but_preserves_a_verified_phone(self, client) -> None:
        user = _user()
        started = STORE.begin_phone_verification(user.id, "+13059511381", "voice")
        assert started is not None
        code, _ = started
        status, _ = STORE.confirm_phone_verification(user.id, code)
        assert status == "ok"
        STORE.begin_phone_verification(user.id, "+442071838750", "sms")

        response = client.post("/console/settings/phone/cancel")

        assert response.status_code == 303
        user = _user()
        assert user.phone == "+13059511381"
        assert user.phone_verified
        assert user.pending_phone is None
        assert user.phone_code_channel is None
        # The floor survives a cancel on purpose — see cancel_pending's docstring.
        assert user.phone_code_sent_at is not None

    def test_cancel_lets_a_typo_be_replaced_once_the_floor_lapses(
        self, client, carrier, monkeypatch
    ) -> None:
        # Cancel is immediate; the NEXT send still waits out the floor, because
        # otherwise cancel-then-start is a way to ring any number continuously.
        import datetime as dt

        from trusted_router import phone_verification as pv
        from trusted_router.storage_models import utcnow

        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )
        cancelled = client.post("/console/settings/phone/cancel")
        assert cancelled.status_code == 303
        assert _user().pending_phone is None

        later = utcnow() + dt.timedelta(seconds=pv.RESEND_FLOOR_SECONDS + 1)
        monkeypatch.setattr(pv, "utcnow", lambda: later)
        restarted = client.post(
            "/console/settings/phone/start",
            data={"phone": "+442071838750", "channel": "voice", "sms_consent": "yes"},
        )

        assert restarted.headers["location"].endswith("sent=voice")
        assert len(carrier.sent) == 2
        assert _user().pending_phone == "+442071838750"


class TestSmsConsentIsRealAndVerifiable:
    """10DLC campaign 30909: rejected for a Call to Action that could not be
    verified. The honest cause was that the consent checkbox described in the
    campaign submission — and promised verbatim on the public /sms page — did not
    exist in the console at all. These tests exist so that cannot recur.
    """

    ANCHOR = (
        "I agree to receive account alerts and one-time verification codes "
        "from Trusted Router at this number."
    )

    @staticmethod
    def _flat(text: str) -> str:
        import re

        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))

    def test_the_form_has_a_required_consent_checkbox(self, client) -> None:
        page = client.get("/console/settings")

        assert 'name="sms_consent"' in page.text, "no consent checkbox in the form"
        assert 'type="checkbox"' in page.text
        # `required` keeps an honest user from missing it; the server check below
        # is what makes it a gate.
        checkbox = page.text.split('name="sms_consent"')[0].rsplit("<input", 1)[-1] + \
            page.text.split('name="sms_consent"')[1].split(">")[0]
        assert "required" in checkbox

    def test_a_number_cannot_be_submitted_without_consent(self, client, carrier) -> None:
        # The gate. A checkbox enforced only in the browser is decoration: this
        # posts the form directly, exactly as anyone could.
        response = client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice"},
        )

        assert not carrier.sent, "a code was sent to a number that never consented"
        assert "error=consent" in response.headers.get("location", "")

    def test_consent_must_be_affirmative_not_merely_present(self, client, carrier) -> None:
        for value in ("", "no", "false", "0", "maybe"):
            carrier.sent.clear()
            client.post(
                "/console/settings/phone/start",
                data={"phone": "+13059511381", "channel": "voice", "sms_consent": value},
            )
            assert not carrier.sent, f"sms_consent={value!r} was treated as consent"

    def test_with_consent_the_code_is_sent(self, client, carrier) -> None:
        response = client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
        )

        assert response.status_code == 303, response.text
        assert carrier.sent, "consent given and still no code sent"

    def test_the_console_and_the_public_page_show_THE_SAME_wording(self, client) -> None:
        """The /sms page tells a campaign vetter these are "the exact steps and
        the exact consent language shown". If the two ever differ, the public
        claim is false and the CTA fails review again — which is what happened.
        """
        console = self._flat(client.get("/console/settings").text)
        public = self._flat(client.get("/sms").text)

        assert self.ANCHOR in console, "console checkbox lost the agreed wording"
        assert self.ANCHOR in public, "public /sms page lost the agreed wording"

    def test_both_places_carry_every_required_disclosure(self, client) -> None:
        # CTIA wants sender, frequency, cost, stop, help, and both policies at
        # the point of collection.
        for path in ("/console/settings", "/sms"):
            flat = self._flat(client.get(path).text)
            for required in (
                "Trusted Router",
                "Message frequency varies",
                "Message and data rates may apply",
                "STOP",
                "HELP",
                "Terms of Service",
                "Privacy Policy",
            ):
                assert required in flat, f"{path} is missing {required!r}"

