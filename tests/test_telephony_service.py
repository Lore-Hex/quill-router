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
        # Most tests exercise only the synchronous carrier request. Leaving
        # repeats enabled starts a 45-second daemon thread that outlives the
        # per-test HTTP mocks and can leak requests into unrelated tests.
        notify_voice_repeat_unanswered=False,
    )
    base.update(overrides)
    return Settings(**base)


def _sms_text(call) -> str:
    """The message body, whichever carrier sent it: Telnyx says `text`,
    Twilio says `Body`."""
    _url, payload, _headers = call
    return payload.get("text") or payload.get("Body") or ""


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


def test_sms_prefers_the_registered_carrier(calls):
    # A2P 10DLC registration is PER CARRIER, so for SMS the default is whoever
    # is actually registered — an unregistered carrier cannot deliver a US
    # message at all, no matter how cheap it is.
    service = telephony.TelephonyService(_settings())
    result = service.send("sms", "+15551234567", "disk full")

    assert result.delivered
    assert result.carrier == "twilio"
    assert not result.failed_primary
    url, payload, _headers = calls[0]
    assert "api.twilio.com" in url
    assert payload["To"] == "+15551234567"


def test_voice_still_prefers_the_cheaper_carrier(calls):
    # Voice needs no registration, so it goes to whoever costs less.
    service = telephony.TelephonyService(
        _settings(telnyx_texml_account_id="acct-1", telnyx_texml_application_id="app-1")
    )

    assert service.send("voice", "+15551234567", "fire").carrier == "telnyx"


def test_sms_falls_over_to_the_other_carrier_and_flags_it(monkeypatch, calls):
    service = telephony.TelephonyService(_settings())
    monkeypatch.setattr(service, "_twilio_sms", lambda to, body: (500, "twilio down"))

    result = service.send("sms", "+15551234567", "disk full")

    assert result.delivered
    assert result.carrier == "telnyx"
    # A send that only worked because of the fallback is NOT the same as a
    # healthy send, and the caller must be able to tell them apart. It matters
    # more for SMS: the fallback carrier may not be registered, so a delivered
    # flag here can still mean the message was dropped downstream.
    assert result.failed_primary
    assert any("twilio=500" in attempt for attempt in result.attempts)


def test_voice_prefers_telnyx_but_falls_back_to_inline_twiml(monkeypatch, calls):
    # Telnyx is cheaper so it leads. Its voice path is TeXML, which fetches
    # instructions FROM US — so when TrustedRouter is unreachable (the very
    # situation being called about) it cannot build the call, and Twilio's
    # inline TwiML, which needs nothing of ours, has to catch it.
    service = telephony.TelephonyService(
        _settings(telnyx_texml_account_id="acct-1", telnyx_texml_application_id="app-1")
    )
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


class TestTelnyxVoiceRequestShape:
    """Both of these were found by placing a real call, not by a test."""

    def test_the_call_carries_an_application_sid(self, calls):
        # Telnyx answers 422 "Missing required parameter ApplicationSid" without
        # it, so a voice path that omits it never rings anyone — and the failure
        # only shows up against the live API.
        service = telephony.TelephonyService(
            _settings(telnyx_texml_account_id="acct-1", telnyx_texml_application_id="app-1")
        )

        result = service.send("voice", "+15551234567", "disk full")

        assert result.carrier == "telnyx"
        url, payload, _headers = calls[0]
        assert "/texml/Accounts/acct-1/Calls" in url
        assert payload["ApplicationSid"] == "app-1"

    def test_texml_url_uses_the_public_control_plane_not_the_api_gateway(self, calls):
        service = telephony.TelephonyService(
            _settings(
                environment="production",
                api_base_url="https://api.trustedrouter.com/v1",
                trusted_domain="trustedrouter.com",
                telnyx_texml_account_id="acct-1",
                telnyx_texml_application_id="app-1",
                internal_gateway_token="prod-token",  # noqa: S106 - test config.
                stripe_webhook_secret="whsec_test",  # noqa: S106 - test config.
                stripe_secret_key="sk_test",  # noqa: S106 - test config.
                sentry_dsn="https://example@example.ingest.sentry.io/1",
                aws_access_key_id="test-access-key",
                aws_secret_access_key="test-secret-key",  # noqa: S106 - test config.
                ses_from_email="noreply@example.com",
                storage_backend="spanner-bigtable",
                spanner_instance_id="trusted-router",
                spanner_database_id="trusted-router",
                bigtable_instance_id="trusted-router-logs",
                byok_kms_key_name=("projects/test/locations/global/keyRings/tr/cryptoKeys/byok"),
            )
        )

        service._telnyx_voice("+15551234567", "verification code")

        _endpoint, payload, _headers = calls[0]
        assert payload["Url"].startswith("https://trustedrouter.com/notify/texml")
        assert not payload["Url"].startswith("https://api.trustedrouter.com")

    def test_voice_is_unconfigured_without_the_application(self):
        # Better to report unconfigured and fall through to the other carrier
        # than to spend a request learning it from a 422.
        service = telephony.TelephonyService(_settings(telnyx_texml_account_id="acct-1"))
        status, detail = service._telnyx_voice("+15551234567", "hi")

        assert status == 0
        assert "TR_TELNYX_TEXML_APPLICATION_ID" in detail


class TestBranding:
    """A number nobody recognizes reading an unattributed sentence at 3am is
    indistinguishable from a scam, and gets hung up on."""

    def test_every_sms_opens_with_the_brand(self, calls):
        telephony.TelephonyService(_settings()).send("sms", "+15551234567", "disk full")

        text = _sms_text(calls[0])
        assert text.startswith("Trusted Router: ")
        assert "disk full" in text

    def test_every_call_opens_with_the_brand(self, calls):
        telephony.TelephonyService(_settings()).send("voice", "+15551234567", "disk full")

        _url, payload, _headers = calls[0]
        assert payload["Twiml"].index("Trusted Router") < payload["Twiml"].index("disk full")

    def test_branding_is_not_applied_twice(self, calls):
        # Agents that already brand their own text must not produce
        # "Trusted Router: Trusted Router: ...".
        telephony.TelephonyService(_settings()).send(
            "sms", "+15551234567", "Trusted Router: disk full"
        )

        assert _sms_text(calls[0]).lower().count("trusted router") == 1

    def test_the_brand_is_spoken_as_a_sentence_not_a_colon(self):
        # "Trusted Router colon disk full" is what a naive prefix would produce
        # through a speech engine.
        spoken = telephony.spoken_text(telephony.branded("disk full"))

        assert spoken.startswith("Trusted Router notification.")
        assert ":" not in spoken
        assert spoken.count("disk full") == 2


class TestPerChannelCarrierDefaults:
    def test_the_defaults_differ_by_channel(self, calls):
        # Not a style choice: SMS must go to the 10DLC-registered carrier or it
        # is rejected outright, while voice is free to chase price.
        settings = _settings(telnyx_texml_account_id="a", telnyx_texml_application_id="b")
        service = telephony.TelephonyService(settings)

        assert service.send("sms", "+1555", "x").carrier == "twilio"
        assert service.send("voice", "+1555", "x").carrier == "telnyx"

    def test_an_explicit_preference_still_wins(self, calls):
        service = telephony.TelephonyService(_settings())
        assert service.send("sms", "+1555", "x", preferred_carrier="telnyx").carrier == "telnyx"

    def test_the_default_is_a_preference_not_an_exclusion(self, monkeypatch, calls):
        service = telephony.TelephonyService(_settings())
        monkeypatch.setattr(service, "_twilio_sms", lambda to, body: (500, "down"))

        assert service.send("sms", "+1555", "x").delivered


class TestUnansweredVoiceRepeats:
    """A single unanswered call is not a delivered page.

    iOS silences unknown numbers and so does Do Not Disturb, but both let a
    repeat call from the same number within three minutes ring through. That is
    the only reliable way to reach a sleeping person from a number they have not
    saved — which is the entire situation a pager exists for.
    """

    def test_a_voice_page_schedules_one_repeat(self, monkeypatch, calls):
        service = telephony.TelephonyService(
            _settings(
                telnyx_texml_account_id="a",
                telnyx_texml_application_id="b",
                notify_voice_repeat_unanswered=True,
            )
        )
        scheduled: list[tuple] = []
        monkeypatch.setattr(
            service, "_repeat_if_unanswered",
            lambda carrier, to, body: scheduled.append((carrier, to)),
        )

        service.send("voice", "+15551234567", "region down")

        assert scheduled == [("telnyx", "+15551234567")]

    def test_sms_is_never_repeated(self, monkeypatch, calls):
        # A text sits on the screen until read. Repeating it is just noise.
        service = telephony.TelephonyService(_settings())
        scheduled: list[tuple] = []
        monkeypatch.setattr(
            service, "_repeat_if_unanswered",
            lambda carrier, to, body: scheduled.append((carrier, to)),
        )

        service.send("sms", "+15551234567", "region down")

        assert scheduled == []

    def test_an_undelivered_call_schedules_nothing(self, monkeypatch, calls):
        # Nothing rang, so there is nothing to repeat — the failover already
        # tried every carrier.
        service = telephony.TelephonyService(_settings())
        monkeypatch.setattr(service, "_telnyx_voice", lambda to, body: (500, "down"))
        monkeypatch.setattr(service, "_twilio_voice", lambda to, body: (500, "down"))
        scheduled: list[tuple] = []
        monkeypatch.setattr(
            service, "_repeat_if_unanswered",
            lambda carrier, to, body: scheduled.append((carrier, to)),
        )

        assert not service.send("voice", "+15551234567", "x").delivered
        assert scheduled == []

    def test_the_repeat_can_be_switched_off(self, monkeypatch):
        service = telephony.TelephonyService(
            _settings(notify_voice_repeat_unanswered=False)
        )
        started: list[str] = []
        monkeypatch.setattr(telephony.threading, "Thread",
                            lambda **kw: started.append(kw.get("name")) or _NeverStarts())

        service._repeat_if_unanswered("telnyx", "+1555", "x")

        assert started == []

    def test_unknown_answer_state_counts_as_unanswered(self):
        # Being wrong this way rings a phone once more; being wrong the other
        # way leaves an incident unreported, and only one of those is
        # recoverable.
        service = telephony.TelephonyService(_settings())

        assert service._voice_was_answered("telnyx", "+15551234567") is False

    def test_the_repeat_runs_off_the_request_path(self, monkeypatch):
        # A voice page must not hold its caller for the length of a ring.
        service = telephony.TelephonyService(
            _settings(notify_voice_repeat_unanswered=True)
        )
        made: list[dict] = []
        monkeypatch.setattr(telephony.threading, "Thread",
                            lambda **kw: made.append(kw) or _NeverStarts())

        service._repeat_if_unanswered("telnyx", "+1555", "x")

        assert made and made[0]["daemon"] is True


class _NeverStarts:
    def start(self) -> None:
        pass
