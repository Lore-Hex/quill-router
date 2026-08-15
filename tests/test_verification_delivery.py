"""Delivering a verification code, and the settings routes around it.

Voice is not a nicety here. A2P 10DLC registration is per carrier and takes
days, and an unregistered sender's SMS is rejected outright — but a phone call
needs no registration. Voice is therefore the only path by which a phone can be
verified while registration is pending, and every gate downstream (notify,
email, SMS itself) is unreachable until one is.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import notify as notify_module
from trusted_router.services.notify import send_verification_code, spoken_code
from trusted_router.services.telephony import TelephonyResult
from trusted_router.storage import STORE


def _settings(**overrides) -> Settings:
    base = dict(
        notify_enabled=True,
        telnyx_api_key="KEY_test",
        telnyx_from_number="+15550000001",
        require_auth=False,
    )
    base.update(overrides)
    return Settings(**base)


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


class TestSpokenCode:
    def test_digits_are_spaced_so_they_are_read_one_by_one(self) -> None:
        # Unspaced, a speech engine says "one hundred twenty-three thousand
        # four hundred fifty-six", which nobody can write down.
        assert "1 2 3 4 5 6" in spoken_code("123456")

    def test_the_code_is_repeated(self) -> None:
        # The listener is reaching for a pen the first time.
        assert spoken_code("123456").count("1 2 3 4 5 6") == 2


class TestSendVerificationCode:
    def test_voice_delivery_speaks_the_code(self, monkeypatch) -> None:
        fake = _Telephony()
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)

        delivered, _detail = send_verification_code(
            _settings(), "+13059511381", "123456", channel="voice"
        )

        assert delivered
        channel, to, body = fake.sent[0]
        assert channel == "voice"
        assert to == "+13059511381"
        assert "1 2 3 4 5 6" in body

    def test_sms_delivery_states_the_expiry(self, monkeypatch) -> None:
        fake = _Telephony()
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)

        send_verification_code(_settings(), "+13059511381", "123456", channel="sms")

        _channel, _to, body = fake.sent[0]
        assert "123456" in body
        assert "10 minutes" in body

    def test_no_carrier_is_reported_rather_than_pretended(self) -> None:
        delivered, detail = send_verification_code(
            Settings(notify_enabled=True), "+13059511381", "123456"
        )
        assert not delivered
        assert "no carrier" in detail

    def test_the_code_never_reaches_a_log(self, monkeypatch, caplog) -> None:
        # A verification code in a log file is a verification code an operator
        # can replay. The carrier's detail is what callers log, never the body.
        fake = _Telephony(delivered=False)
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)

        with caplog.at_level(logging.DEBUG):
            send_verification_code(_settings(), "+13059511381", "857321", channel="voice")

        assert "857321" not in caplog.text


class TestSettingsRoutes:
    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(create_app(_settings(), init_observability=False))

    def test_a_code_can_be_delivered_by_voice_and_then_confirmed(
        self, client, user_headers, monkeypatch
    ) -> None:
        # The whole point: this works with no SMS registration anywhere.
        fake = _Telephony()
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)

        started = client.post(
            "/notify/phone/start",
            headers=user_headers,
            json={"phone": "+1 (305) 951-1381", "channel": "voice"},
        )
        assert started.status_code == 200, started.text
        assert started.json()["channel"] == "voice"

        _channel, _to, body = fake.sent[0]
        code = "".join(ch for ch in body.split("is")[1] if ch.isdigit())[:6]

        confirmed = client.post("/notify/phone/confirm", headers=user_headers, json={"code": code})

        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["verified"] is True
        assert confirmed.json()["phone"] == "+13059511381"

    def test_a_malformed_number_is_rejected_before_any_carrier_is_called(
        self, client, user_headers, monkeypatch
    ) -> None:
        fake = _Telephony()
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)

        response = client.post(
            "/notify/phone/start", headers=user_headers, json={"phone": "3059511381"}
        )

        assert response.status_code == 400
        assert fake.sent == [], "a carrier was called with a number we knew was bad"

    def test_an_immediate_resend_is_refused(self, client, user_headers, monkeypatch) -> None:
        # Without this, "start verification" is a way to ring someone else's
        # phone over and over.
        fake = _Telephony()
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)
        body = {"phone": "+13059511381", "channel": "voice"}

        assert client.post("/notify/phone/start", headers=user_headers, json=body).status_code == 200
        second = client.post("/notify/phone/start", headers=user_headers, json=body)

        assert second.status_code == 429
        assert "retry-after" in second.headers
        assert len(fake.sent) == 1, "the resend floor did not stop a second call"

    def test_a_wrong_code_does_not_verify(self, client, user_headers, monkeypatch) -> None:
        fake = _Telephony()
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda s: fake)
        client.post(
            "/notify/phone/start",
            headers=user_headers,
            json={"phone": "+13059511381", "channel": "voice"},
        )

        response = client.post("/notify/phone/confirm", headers=user_headers, json={"code": "000000"})

        assert response.status_code == 400
        assert response.json()["verified"] is False

    def test_a_failed_send_keeps_the_pending_code_for_a_retry(
        self, client, user_headers, monkeypatch
    ) -> None:
        # A carrier blip must not look like a rejected number, or the user
        # starts over and burns the resend floor for nothing.
        monkeypatch.setattr(
            notify_module, "get_telephony_service", lambda s: _Telephony(delivered=False)
        )

        response = client.post(
            "/notify/phone/start",
            headers=user_headers,
            json={"phone": "+13059511381", "channel": "voice"},
        )

        assert response.status_code == 502
        user = STORE.find_user_by_email("alice@example.com")
        assert user is not None
        assert user.pending_phone == "+13059511381"
