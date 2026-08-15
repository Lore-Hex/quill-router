"""Owner notifications: the gate, the pricing, and what is billable.

The destination is never a parameter, so the tests that matter are about who
may be reached, when, and what it costs when delivery fails.
"""

from __future__ import annotations

import pytest

from trusted_router import phone_verification as pv
from trusted_router.config import Settings
from trusted_router.services import notify as notify_module
from trusted_router.services.notify import NotifyService
from trusted_router.storage_models import User


def _settings(**overrides) -> Settings:
    base = dict(
        notify_enabled=True,
        telnyx_api_key="KEY_test",
        telnyx_from_number="+15550000001",
    )
    base.update(overrides)
    return Settings(**base)


def _verified_owner(email: str | None = "owner@example.com", email_verified: bool = True) -> User:
    user = User(id="u1", email=email, email_verified=email_verified)
    code = pv.begin(user, "+13059511381")
    pv.confirm(user, code)
    return user


def _unverified_owner() -> User:
    return User(id="u2", email="owner@example.com", email_verified=True)


class TestTheGate:
    @pytest.mark.parametrize("channel", ["push", "email", "sms", "voice"])
    def test_every_channel_requires_a_verified_phone(self, channel):
        # Including email, which costs us a hundredth of a cent. The phone is
        # not protecting the recipient — there is only one possible recipient —
        # it is a cost floor on account farming.
        service = NotifyService(_settings())

        outcome = service.send(
            owner=_unverified_owner(), channel=channel, subject="s", body="b",
            push_sender=lambda *a: (True, "ok"),
        )

        assert not outcome.delivered
        assert outcome.refusal == "phone_not_verified"
        assert outcome.price_microdollars == 0

    def test_a_pending_phone_does_not_count_as_verified(self):
        user = _unverified_owner()
        pv.begin(user, "+13059511381")  # started, never confirmed

        outcome = NotifyService(_settings()).send(
            owner=user, channel="sms", subject="", body="hello"
        )

        assert outcome.refusal == "phone_not_verified"

    def test_email_additionally_requires_a_verified_email(self):
        service = NotifyService(_settings())

        no_email = service.send(
            owner=_verified_owner(email=None), channel="email", subject="s", body="b"
        )
        unverified = service.send(
            owner=_verified_owner(email_verified=False), channel="email", subject="s", body="b"
        )

        assert no_email.refusal == "email_not_attached"
        assert unverified.refusal == "email_not_verified"

    def test_no_owner_is_refused(self):
        outcome = NotifyService(_settings()).send(
            owner=None, channel="sms", subject="s", body="b"
        )
        assert outcome.refusal == "no_owner"

    def test_an_empty_body_is_refused(self):
        # A notification with no content wastes a page and teaches its reader
        # to ignore the next one.
        outcome = NotifyService(_settings()).send(
            owner=_verified_owner(), channel="sms", subject="s", body="   "
        )
        assert outcome.refusal == "empty_body"

    def test_the_gate_can_be_checked_without_spending_anything(self):
        assert notify_module.check_owner_reachable(_verified_owner(), "sms") is None
        assert notify_module.check_owner_reachable(_unverified_owner(), "sms") is not None


class TestPricing:
    def test_push_is_free_and_the_others_are_not(self):
        settings = _settings()
        assert notify_module.price_for("push", settings) == 0
        for channel in ("email", "sms", "voice"):
            assert notify_module.price_for(channel, settings) == settings.notify_price_microdollars

    def test_email_costs_the_same_as_a_phone_send(self):
        # Pricing email lower would make it the obvious channel to abuse, and
        # would train customers to route around the expensive one.
        settings = _settings()
        assert notify_module.price_for("email", settings) == notify_module.price_for("sms", settings)

    def test_a_delivered_send_is_billable(self, monkeypatch):
        service = NotifyService(_settings())
        monkeypatch.setattr(
            notify_module, "get_telephony_service",
            lambda settings: _FakeTelephony(delivered=True),
        )

        outcome = service.send(owner=_verified_owner(), channel="sms", subject="", body="hi")

        assert outcome.delivered and outcome.billable
        assert outcome.price_microdollars == _settings().notify_price_microdollars

    def test_a_failed_send_is_not_billable(self, monkeypatch):
        # A carrier outage must not charge the customer: they asked for their
        # human to be reached and their human was not reached. It would also
        # make an outage look like revenue.
        service = NotifyService(_settings())
        monkeypatch.setattr(
            notify_module, "get_telephony_service",
            lambda settings: _FakeTelephony(delivered=False),
        )

        outcome = service.send(owner=_verified_owner(), channel="sms", subject="", body="hi")

        assert not outcome.delivered
        assert not outcome.billable
        assert outcome.price_microdollars == 0

    def test_delivered_push_is_free_but_not_billable(self):
        outcome = NotifyService(_settings()).send(
            owner=_verified_owner(), channel="push", subject="s", body="b",
            push_sender=lambda owner, subject, body: (True, "delivered"),
        )

        assert outcome.delivered
        assert outcome.price_microdollars == 0
        assert not outcome.billable


class TestDelivery:
    def test_push_without_a_registered_device_points_at_the_app(self):
        outcome = NotifyService(_settings()).send(
            owner=_verified_owner(), channel="push", subject="s", body="b",
            push_sender=lambda owner, subject, body: (False, "no registered device"),
        )

        assert not outcome.delivered
        assert outcome.refusal == "no_push_device"

    def test_push_is_unavailable_when_the_deployment_has_no_sender(self):
        outcome = NotifyService(_settings()).send(
            owner=_verified_owner(), channel="push", subject="s", body="b"
        )
        assert outcome.refusal == "channel_unavailable"

    def test_a_carrier_preference_is_passed_through(self, monkeypatch):
        fake = _FakeTelephony(delivered=True)
        monkeypatch.setattr(notify_module, "get_telephony_service", lambda settings: fake)

        NotifyService(_settings()).send(
            owner=_verified_owner(), channel="sms", subject="", body="hi",
            preferred_carrier="twilio",
        )

        assert fake.last_preference == "twilio"

    def test_unknown_channel_is_refused(self):
        outcome = NotifyService(_settings()).send(
            owner=_verified_owner(), channel="carrier-pigeon", subject="", body="hi"
        )
        assert outcome.refusal == "unknown_channel"


class _FakeTelephony:
    def __init__(self, *, delivered: bool) -> None:
        self._delivered = delivered
        self.enabled = True
        self.last_preference: str | None = None

    def send(self, channel, to, body, preferred_carrier=None):
        from trusted_router.services.telephony import TelephonyResult

        self.last_preference = preferred_carrier
        if self._delivered:
            return TelephonyResult(True, "telnyx", "queued")
        return TelephonyResult(False, None, "telnyx=500; twilio=500")


class TestEmailIdentity:
    """Notify pages from the ALERT identity, not the default sender."""

    def test_it_sends_from_the_alert_profile(self, monkeypatch):
        # Operational paging and receipts must not share SES reputation: a
        # bounce storm from one would degrade delivery of the other, and
        # paging is the half that has to arrive at 3am.
        captured = {}

        class _Email:
            enabled = True

            def send(self, message):
                captured["profile"] = message.sender_profile
                captured["to"] = message.to
                return True

        monkeypatch.setattr(notify_module, "get_email_service", lambda settings: _Email())

        outcome = NotifyService(_settings()).send(
            owner=_verified_owner(), channel="email", subject="region down", body="six minutes"
        )

        assert outcome.delivered
        assert captured["profile"] == "alerts"
        assert captured["to"] == "owner@example.com"

    def test_a_suppressed_address_is_reported_undelivered_and_unbilled(self, monkeypatch):
        # SES refuses addresses on our bounce/complaint suppression list. That
        # is a real non-delivery, so it must not be charged.
        class _Email:
            enabled = True

            def send(self, message):
                return False

        monkeypatch.setattr(notify_module, "get_email_service", lambda settings: _Email())

        outcome = NotifyService(_settings()).send(
            owner=_verified_owner(), channel="email", subject="s", body="b"
        )

        assert not outcome.delivered
        assert not outcome.billable
        assert outcome.price_microdollars == 0
