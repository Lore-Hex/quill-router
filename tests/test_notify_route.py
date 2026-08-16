"""POST /v1/notify — auth, the gate, and where the money goes.

The service's own rules are tested in test_notify_service.py. What matters here
is the wiring: that a caller cannot choose a destination, that a refusal is
distinguishable from a failure, and above all that a page nobody received is a
page nobody paid for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import notify as notify_module
from trusted_router.services.telephony import TelephonyResult
from trusted_router.storage import STORE
from trusted_router.typed_balance import live_credit_summary


@pytest.fixture
def notify_settings() -> Settings:
    return Settings(
        notify_enabled=True,
        telnyx_api_key="KEY_test",
        telnyx_from_number="+15550000001",
        require_auth=False,
    )


@pytest.fixture
def notify_client(notify_settings: Settings) -> TestClient:
    return TestClient(create_app(notify_settings, init_observability=False))


def _owner_of_key(client: TestClient, headers: dict[str, str]):
    """The user the api key's workspace belongs to — the only possible
    destination, which is the point of the design."""
    key = STORE.get_key_by_raw(headers["authorization"].split()[1])
    assert key is not None
    workspace = STORE.get_workspace(key.workspace_id)
    assert workspace is not None
    return STORE.get_user(workspace.owner_user_id)


def _verify_phone(user) -> None:
    """Through the store, the way settings will — mutating a returned copy
    would verify a phone that no backend ever persisted."""
    started = STORE.begin_phone_verification(user.id, "+13059511381")
    assert started is not None
    code, _ = started
    status, _ = STORE.confirm_phone_verification(user.id, code)
    assert status == "ok"


class _Telephony:
    def __init__(self, *, delivered: bool) -> None:
        self._delivered = delivered
        self.enabled = True

    def send(self, channel, to, body, preferred_carrier=None):
        if self._delivered:
            return TelephonyResult(True, "telnyx", "queued")
        return TelephonyResult(False, None, "telnyx=500; twilio=500")


class TestAuth:
    def test_an_api_key_is_required(self, notify_client: TestClient) -> None:
        response = notify_client.post("/v1/notify", json={"channel": "sms", "body": "hi"})
        assert response.status_code in (401, 403)

    def test_the_caller_cannot_name_a_destination(
        self, notify_client, inference_headers, monkeypatch
    ) -> None:
        # The whole safety argument: an ordinary inference key cannot be aimed
        # at a stranger, so "to" is not a parameter and must be ignored if sent.
        sent_to: list[str] = []

        class _Recorder(_Telephony):
            def send(self, channel, to, body, preferred_carrier=None):
                sent_to.append(to)
                return TelephonyResult(True, "telnyx", "queued")

        owner = _owner_of_key(notify_client, inference_headers)
        _verify_phone(owner)
        monkeypatch.setattr(
            notify_module, "get_telephony_service", lambda s: _Recorder(delivered=True)
        )

        notify_client.post(
            "/v1/notify",
            headers=inference_headers,
            json={"channel": "sms", "body": "hi", "to": "+15559999999"},
        )

        assert sent_to == ["+13059511381"], "a caller-supplied destination was honoured"


class TestTheGate:
    def test_an_unverified_owner_is_refused_and_charged_nothing(
        self, notify_client, inference_headers
    ) -> None:
        response = notify_client.post(
            "/v1/notify", headers=inference_headers, json={"channel": "sms", "body": "hi"}
        )

        assert response.status_code == 409
        assert response.json()["refusal"] == "phone_not_verified"
        assert response.json()["charged_microdollars"] == 0

    def test_an_unknown_channel_is_a_bad_request_not_a_conflict(
        self, notify_client, inference_headers
    ) -> None:
        # A caller can fix a typo; it should not look like a state problem they
        # are meant to wait out.
        response = notify_client.post(
            "/v1/notify", headers=inference_headers, json={"channel": "telepathy", "body": "hi"}
        )
        assert response.status_code == 400


class TestMoney:
    def test_a_delivered_send_is_charged(
        self, notify_client, inference_headers, monkeypatch, notify_settings
    ) -> None:
        owner = _owner_of_key(notify_client, inference_headers)
        _verify_phone(owner)
        monkeypatch.setattr(
            notify_module, "get_telephony_service", lambda s: _Telephony(delivered=True)
        )

        response = notify_client.post(
            "/v1/notify", headers=inference_headers, json={"channel": "sms", "body": "disk full"}
        )

        assert response.status_code == 200
        assert response.json()["delivered"] is True
        assert response.json()["charged_microdollars"] == notify_settings.notify_price_microdollars

    def test_a_failed_send_costs_nothing_and_leaves_no_reservation(
        self, notify_client, inference_headers, monkeypatch
    ) -> None:
        # The one that matters. A reserve that is never settled OR refunded
        # silently eats the customer's credit and shows up as usage nobody
        # can explain — so assert the balance itself, not just the response.
        owner = _owner_of_key(notify_client, inference_headers)
        _verify_phone(owner)
        key = STORE.get_key_by_raw(inference_headers["authorization"].split()[1])
        assert key is not None
        before = live_credit_summary(key.workspace_id)
        monkeypatch.setattr(
            notify_module, "get_telephony_service", lambda s: _Telephony(delivered=False)
        )

        response = notify_client.post(
            "/v1/notify", headers=inference_headers, json={"channel": "sms", "body": "disk full"}
        )

        assert response.status_code == 502
        assert response.json()["charged_microdollars"] == 0
        after = live_credit_summary(key.workspace_id)
        assert after["reserved"] == before["reserved"], "reservation leaked"
        assert after["total_usage"] == before["total_usage"], "an undelivered page was billed"
        assert after["available"] == before["available"]

    def test_push_is_free(self, notify_client, inference_headers) -> None:
        owner = _owner_of_key(notify_client, inference_headers)
        _verify_phone(owner)

        response = notify_client.post(
            "/v1/notify", headers=inference_headers, json={"channel": "push", "body": "hi"}
        )

        # No push sender configured in tests, so this refuses — but free either
        # way, and it must never reserve credit.
        assert response.json()["charged_microdollars"] == 0


class TestTexml:
    def test_the_instructions_are_public_and_branded(self, notify_client: TestClient) -> None:
        # A carrier fetches this from its own infrastructure holding no
        # credential of ours, so it cannot be authenticated.
        response = notify_client.get("/notify/texml", params={"text": "region two is down"})

        assert response.status_code == 200
        assert "Trusted Router" in response.text
        assert response.text.count("region two is down") == 2

    def test_post_returns_the_same_instructions_and_ignores_form_status(
        self, notify_client: TestClient
    ) -> None:
        query = {"text": "region two is down"}
        get_response = notify_client.get("/notify/texml", params=query)

        post_response = notify_client.post(
            "/notify/texml",
            params=query,
            data={"CallStatus": "in-progress", "From": "+15551234567"},
        )

        assert post_response.status_code == 200
        assert post_response.text == get_response.text

    def test_xml_metacharacters_cannot_break_the_document(self, notify_client: TestClient) -> None:
        # A document that fails to parse is a call that connects and says
        # nothing — indistinguishable from a page that never arrived.
        response = notify_client.get("/notify/texml", params={"text": "a <b> & </Say>"})

        assert response.text.count("<Say") == 1
        assert response.text.count("</Say>") == 1
