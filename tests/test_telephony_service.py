"""Carrier delivery for notifications.

Every failure here is silent from our side — a carrier returns a status code
and the customer's phone either rings or does not — so the request shape and
the failover are asserted rather than trusted.
"""

from __future__ import annotations

import base64

import pytest

from trusted_router.config import Settings
from trusted_router.services import telephony


def _settings(**overrides) -> Settings:
    base = dict(
        notify_enabled=True,
        telnyx_api_key="KEY_test",
        telnyx_from_number="+15550000001",
        twilio_account_sid="ACtest",
        twilio_api_key_sid="SKtest",
        twilio_api_key_secret="secret",  # noqa: S106
        twilio_from_number="+15550000002",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, dict, dict]] = []

    def fake_form(url, data, headers):
        seen.append((url, data, headers))
        return 201, "{}"

    def fake_json(url, payload, headers):
        seen.append((url, payload, headers))
        return 200, "{}"

    monkeypatch.setattr(telephony, "_post_form", fake_form)
    monkeypatch.setattr(telephony, "_post_json", fake_json)
    return seen


def test_sms_prefers_telnyx(calls):
    service = telephony.TelephonyService(_settings())
    result = service.send("sms", "+15551234567", "disk full")

    assert result.delivered
    assert result.carrier == "telnyx"
    assert not result.failed_primary
    url, payload, _headers = calls[0]
    assert "api.telnyx.com" in url
    assert payload["to"] == "+15551234567"


def test_sms_falls_over_to_twilio_and_flags_it(monkeypatch, calls):
    service = telephony.TelephonyService(_settings())
    monkeypatch.setattr(service, "_telnyx_sms", lambda to, body: (500, "telnyx down"))

    result = service.send("sms", "+15551234567", "disk full")

    assert result.delivered
    assert result.carrier == "twilio"
    # A send that only worked because of the fallback is NOT the same as a
    # healthy send, and the caller must be able to tell them apart.
    assert result.failed_primary
    assert any("telnyx=500" in attempt for attempt in result.attempts)


def test_voice_prefers_telnyx_but_falls_back_to_inline_twiml(monkeypatch, calls):
    # Telnyx is cheaper so it leads. Its voice path is TeXML, which fetches
    # instructions FROM US — so when TrustedRouter is unreachable (the very
    # situation being called about) it cannot build the call, and Twilio's
    # inline TwiML, which needs nothing of ours, has to catch it.
    service = telephony.TelephonyService(_settings(telnyx_texml_account_id="acct-1"))
    assert service.send("voice", "+15551234567", "fire").carrier == "telnyx"

    monkeypatch.setattr(service, "_telnyx_voice", lambda to, body: (0, "texml unreachable"))
    result = service.send("voice", "+15551234567", "fire")

    assert result.carrier == "twilio"
    assert result.failed_primary
    url, payload, _headers = calls[-1]
    assert "/Calls.json" in url
    assert "<Response><Say" in payload["Twiml"]


def test_a_caller_can_pin_a_carrier(calls):
    service = telephony.TelephonyService(_settings())
    assert service.send("sms", "+1555", "x", preferred_carrier="twilio").carrier == "twilio"
    assert service.send("sms", "+1555", "x", preferred_carrier="telnyx").carrier == "telnyx"


def test_a_preference_is_not_an_exclusion(monkeypatch, calls):
    # Honouring "telnyx only" would turn a preference into a single point of
    # failure the caller never asked for: they want it to ARRIVE.
    service = telephony.TelephonyService(_settings())
    monkeypatch.setattr(service, "_telnyx_sms", lambda to, body: (500, "telnyx down"))

    result = service.send("sms", "+1555", "x", preferred_carrier="telnyx")

    assert result.delivered
    assert result.carrier == "twilio"


def test_twilio_api_key_authenticates_but_the_path_uses_the_account_sid(calls):
    # An SK key authenticates as SID:SECRET while the PATH must carry the AC
    # account sid. Using the key in both places returns 200 from some read
    # endpoints and then fails on send, which is exactly how it looks correct.
    service = telephony.TelephonyService(_settings())
    service.send("voice", "+15551234567", "hello")

    url, _payload, headers = calls[0]
    assert "/Accounts/ACtest/" in url
    decoded = base64.b64decode(headers["Authorization"].split()[1]).decode()
    assert decoded == "SKtest:secret"


def test_account_token_auth_still_works_without_an_api_key(calls):
    account_token = "tok"  # noqa: S105 — a fake credential in a test
    service = telephony.TelephonyService(
        _settings(
            twilio_api_key_sid=None,
            twilio_api_key_secret=None,
            twilio_auth_token=account_token,
        )
    )
    service.send("voice", "+15551234567", "hello")

    _url, _payload, headers = calls[0]
    assert base64.b64decode(headers["Authorization"].split()[1]).decode() == "ACtest:tok"


def test_both_carriers_failing_is_reported_as_undelivered(monkeypatch, calls):
    service = telephony.TelephonyService(_settings())
    monkeypatch.setattr(service, "_telnyx_sms", lambda to, body: (500, "down"))
    monkeypatch.setattr(service, "_twilio_sms", lambda to, body: (0, "ConnectionError"))

    result = service.send("sms", "+15551234567", "nothing works")

    assert not result.delivered
    assert result.carrier is None
    assert "telnyx=500" in result.detail and "twilio=0" in result.detail


def test_no_credentials_reports_disabled_rather_than_pretending():
    service = telephony.TelephonyService(Settings(notify_enabled=True))
    assert not service.enabled
    result = service.send("sms", "+15551234567", "hi")
    assert not result.delivered
    assert "unconfigured" in result.detail


def test_one_carrier_is_enabled_but_not_redundant():
    service = telephony.TelephonyService(
        _settings(twilio_account_sid=None, twilio_api_key_sid=None, twilio_api_key_secret=None)
    )
    assert service.enabled
    # Legal, but a pager with a single point of failure — worth surfacing.
    assert not service.redundant


def test_spoken_text_repeats_and_strips_xml_metacharacters():
    spoken = telephony.spoken_text("a < b & c > d")
    assert spoken.count("a") >= 2
    for character in ("<", ">", "&"):
        assert character not in spoken


def test_spoken_text_repeats_the_message():
    # A ringing phone is answered mid-sentence; the first pass is half heard.
    assert telephony.spoken_text("region two is down").count("region two is down") == 2
