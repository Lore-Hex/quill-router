"""The settings page that actually lets someone add a phone number.

The API existed for a day before this did, which made the feature unreachable
for anyone who was not willing to hand-craft a POST — including its owner, who
went looking for it in the console and found nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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


@pytest.fixture
def client(client: TestClient) -> TestClient:
    """A signed-in console session on conftest's app.

    /console/* rejects API-key Bearer auth and wants the cookie sign-in mints;
    standing up a second create_app would replace process-wide settings and
    poison unrelated tests.
    """
    user = STORE.ensure_user("console-phone@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id, provider="test", label="t", ttl_seconds=3600,
        workspace_id=workspace.id, state="active",
    )
    client.cookies.set("tr_session", raw_session)
    client.follow_redirects = False
    return client


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

    def test_it_offers_a_phone_call_as_well_as_a_text(self, client) -> None:
        # A call needs no carrier registration and reaches landlines, so it must
        # be offered rather than hidden behind SMS failing first.
        page = client.get("/console/settings")

        assert "Phone call" in page.text


class TestVerifying:
    def test_a_code_can_be_requested_and_confirmed(self, client, carrier) -> None:
        started = client.post(
            "/console/settings/phone/start",
            data={"phone": "+1 (305) 951-1381", "channel": "voice"},
        )
        assert started.status_code == 303, started.text
        assert carrier.sent, "no code was sent"

        _channel, _to, spoken = carrier.sent[0]
        code = "".join(ch for ch in spoken.split("is")[1] if ch.isdigit())[:6]

        confirmed = client.post(
            "/console/settings/phone/confirm", data={"code": code}
        )
        assert confirmed.status_code == 303

        user = _user()
        assert user.phone_verified
        assert user.phone == "+13059511381"

    def test_the_page_then_shows_the_verified_number(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice"},
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
            data={"phone": "3059511381", "channel": "voice"},
        )

        assert response.status_code == 303
        assert "error=phone" in response.headers["location"]
        assert carrier.sent == []

    def test_a_wrong_code_does_not_verify(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice"},
        )

        response = client.post(
            "/console/settings/phone/confirm", data={"code": "000000"}
        )

        assert "error=mismatch" in response.headers["location"]
        assert not _user().phone_verified

    def test_an_immediate_resend_is_refused(self, client, carrier) -> None:
        # Otherwise this form rings a stranger's phone as fast as it can be
        # submitted.
        body = {"phone": "+13059511381", "channel": "voice"}
        client.post("/console/settings/phone/start", data=body)

        second = client.post("/console/settings/phone/start", data=body)

        assert "error=rate" in second.headers["location"]
        assert len(carrier.sent) == 1


class TestPostRedirectGet:
    def test_starting_redirects_rather_than_rendering(self, client, carrier) -> None:
        # A refresh on a rendered POST would ring the phone again.
        response = client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice"},
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/console/settings")


class TestRemoval:
    def test_a_number_can_be_removed(self, client, carrier) -> None:
        client.post(
            "/console/settings/phone/start",
            data={"phone": "+13059511381", "channel": "voice"},
        )
        _c, _t, spoken = carrier.sent[0]
        code = "".join(ch for ch in spoken.split("is")[1] if ch.isdigit())[:6]
        client.post("/console/settings/phone/confirm", data={"code": code})

        client.post("/console/settings/phone/remove")

        user = _user()
        assert not user.phone_verified
        assert user.phone is None
