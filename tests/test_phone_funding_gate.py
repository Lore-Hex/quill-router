from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS
from trusted_router.services import notify as notify_module
from trusted_router.services.telephony import TelephonyResult
from trusted_router.storage import STORE
from trusted_router.storage_models import CreditProvenance


class _Telephony:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(
        self,
        channel: str,
        to: str,
        body: str,
        preferred_carrier: str | None = None,
    ) -> TelephonyResult:
        self.sent.append((channel, to, body))
        return TelephonyResult(True, "telnyx", "queued")


def _settings() -> Settings:
    return Settings(
        environment="test",
        phone_verification_requires_funding=True,
    )


def _fund_user(
    user: Any,
    *,
    event_id: str = "evt_phone_gate_funding",
    amount: int = 1,
) -> None:
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_typed_direct(
        workspace.id,
        amount,
        event_id,
        provenance=CreditProvenance.system_grant(),
        lifetime_topup_user_id=user.id,
    )


def _console_client() -> tuple[TestClient, Any]:
    client = TestClient(create_app(_settings(), init_observability=False))
    client.follow_redirects = False
    user = STORE.ensure_user("phone-gate-console@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="phone gate",
        ttl_seconds=3600,
        workspace_id=workspace.id,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)
    return client, user


def test_phone_funding_default_is_off_in_test_and_local() -> None:
    assert Settings(environment="test").phone_verification_funding_enforced is False
    assert Settings(environment="local").phone_verification_funding_enforced is False
    assert (
        Settings(
            environment="staging",
            service_surface="control",
            attribution_cookie_secret="staging-attribution-" + "a" * 32,
            stripe_webhook_secret="whsec_" + "staging",
            stripe_secret_key="sk_" + "staging",
        ).phone_verification_funding_enforced
        is True
    )
    assert (
        Settings(
            environment="test",
            phone_verification_requires_funding=True,
        ).phone_verification_funding_enforced
        is True
    )


def test_api_start_requires_funding_then_allows_any_successful_topup(
    monkeypatch: pytest.MonkeyPatch,
    user_headers: dict[str, str],
) -> None:
    carrier = _Telephony()
    monkeypatch.setattr(notify_module, "get_telephony_service", lambda _settings: carrier)
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        blocked = client.post(
            "/v1/notify/phone/start",
            headers=user_headers,
            json={"phone": "+13059511381", "channel": "voice"},
        )
        user = STORE.find_user_by_email("alice@example.com")
        assert user is not None
        _fund_user(user)
        allowed = client.post(
            "/v1/notify/phone/start",
            headers=user_headers,
            json={"phone": "+13059511381", "channel": "voice"},
        )

    assert blocked.status_code == 403
    assert blocked.json()["error"]["type"] == "verification_required"
    assert blocked.json()["error"]["missing_requirements"] == ["funding"]
    assert blocked.json()["error"]["verification_url"] == "/console/settings"
    assert carrier.sent and allowed.status_code == 200


def test_api_gate_reports_email_before_funding() -> None:
    settings = _settings()
    with TestClient(create_app(settings, init_observability=False)) as client:
        user = STORE.create_wallet_user("0x" + "4" * 40)
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        raw_session, _session = STORE.create_auth_session(
            user_id=user.id,
            provider="wallet",
            label=user.wallet_address or "",
            ttl_seconds=3600,
            workspace_id=workspace.id,
            state="active",
        )
        response = client.post(
            "/v1/notify/phone/start",
            headers={"authorization": f"Bearer {raw_session}"},
            json={"phone": "+13059511381"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["missing_requirements"] == ["email", "funding"]


def test_console_hides_phone_form_and_redirects_to_funding_flash() -> None:
    client, _user = _console_client()

    page = client.get("/console/settings")
    blocked = client.post(
        "/console/settings/phone/start",
        data={"phone": "+13059511381", "channel": "voice"},
    )
    flash = client.get(blocked.headers["location"])

    assert page.status_code == 200
    assert 'name="phone"' not in page.text
    assert "phone verification is available after your first top-up" in page.text
    assert "/console/credits?purpose=identity_verification" in page.text
    assert blocked.status_code == 303
    assert blocked.headers["location"].endswith("error=funding")
    assert "Add credits first" in flash.text


def test_console_identity_checkout_shows_and_preserves_the_nudge() -> None:
    client, _user = _console_client()

    page = client.get("/console/credits?purpose=identity_verification")
    checkout = client.post(
        "/console/credits/checkout",
        data={
            "amount": "1",
            "payment_method": "card",
            "purpose": "identity_verification",
        },
    )

    assert page.status_code == 200
    assert "requires $25.00 more in lifetime top-ups" in page.text
    assert 'name="purpose" value="identity_verification"' in page.text
    assert "purpose=identity_verification" in checkout.headers["location"]


def test_console_start_works_after_any_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, user = _console_client()
    carrier = _Telephony()
    monkeypatch.setattr(notify_module, "get_telephony_service", lambda _settings: carrier)
    _fund_user(user)

    response = client.post(
        "/console/settings/phone/start",
        data={"phone": "+13059511381", "channel": "voice", "sms_consent": "yes"},
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("sent=voice")
    assert len(carrier.sent) == 1


def test_confirm_cancel_and_remove_remain_ungated() -> None:
    client, user = _console_client()
    started = STORE.begin_phone_verification(user.id, "+13059511381", "voice")
    assert started is not None
    code, _updated = started

    confirmed = client.post("/console/settings/phone/confirm", data={"code": code})
    removed = client.post("/console/settings/phone/remove")
    pending = STORE.begin_phone_verification(user.id, "+442071838750", "voice")
    assert pending is not None
    cancelled = client.post("/console/settings/phone/cancel")

    assert confirmed.headers["location"].endswith("phone_saved=1")
    assert removed.status_code == 303
    assert cancelled.status_code == 303
    refreshed = STORE.get_user(user.id)
    assert refreshed is not None
    assert refreshed.phone is None
    assert refreshed.pending_phone is None
    assert STORE.get_lifetime_topup_microdollars(user.id) == 0


def test_verification_status_reports_shape_and_next_step_progression(
    user_headers: dict[str, str],
) -> None:
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        initial_response = client.get(
            "/v1/auth/verification-status",
            headers=user_headers,
        )
        user = STORE.find_user_by_email("alice@example.com")
        assert user is not None
        initial = initial_response.json()["data"]

        STORE.mark_user_email_verified(user.id)
        email_done = client.get(
            "/v1/auth/verification-status",
            headers=user_headers,
        ).json()["data"]

        _fund_user(
            user,
            event_id="evt_verification_status_funding",
            amount=VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
        )
        funded = client.get(
            "/v1/auth/verification-status",
            headers=user_headers,
        ).json()["data"]

        started = STORE.begin_phone_verification(user.id, "+13059511381", "voice")
        assert started is not None
        code, _updated = started
        status, _updated = STORE.confirm_phone_verification(user.id, code)
        assert status == "ok"
        phone_done = client.get(
            "/v1/auth/verification-status",
            headers=user_headers,
        ).json()["data"]

        STORE.set_user_identity_status(user.id, status="approved")
        identity_done = client.get(
            "/v1/auth/verification-status",
            headers=user_headers,
        ).json()["data"]

    assert initial_response.status_code == 200
    assert initial == {
        "email": "alice@example.com",
        "username": None,
        "email_verified": False,
        "lifetime_topup": 0.0,
        "lifetime_topup_microdollars": 0,
        "phone_verified": False,
        "phone": None,
        "identity_status": "none",
        "identity_verified_at": None,
        "veriff_attempt_count": 0,
        # Nothing to say at `none` — guidance appears only after a decision.
        "identity_message": None,
        "verification_fee": 5.0,
        "verification_fee_microdollars": 5_000_000,
        "lifetime_topup_required": 25.0,
        "lifetime_topup_required_microdollars": 25_000_000,
        "missing_requirements": ["phone_verified", "funding"],
        "next_step": "email",
    }
    assert email_done["next_step"] == "funding"
    assert funded["lifetime_topup_microdollars"] == 25_000_000
    assert funded["missing_requirements"] == ["phone_verified"]
    assert funded["next_step"] == "phone"
    assert phone_done["phone_verified"] is True
    assert phone_done["phone"] == "+13059511381"
    assert phone_done["next_step"] == "identity"
    assert identity_done["identity_status"] == "approved"
    assert identity_done["identity_verified_at"] is not None
    assert identity_done["next_step"] is None


def test_verification_status_never_falls_back_to_the_workspace_owner(
    user_headers: dict[str, str],
) -> None:
    # A management key minted under another key has no creator. If the status
    # endpoint answered for the workspace OWNER in that case, any non-owner
    # admin could read the owner's phone number and email through it.
    with TestClient(create_app(_settings(), init_observability=False)) as client:
        client.get("/v1/auth/verification-status", headers=user_headers)
        owner = STORE.find_user_by_email("alice@example.com")
        assert owner is not None
        workspace = STORE.list_workspaces_for_user(owner.id)[0]
        started = STORE.begin_phone_verification(owner.id, "+13059511381", "voice")
        assert started is not None
        code, _ = started
        assert STORE.confirm_phone_verification(owner.id, code)[0] == "ok"

        raw_key, _record = STORE.create_api_key(
            workspace_id=workspace.id,
            name="ownerless-management-key",
            creator_user_id=None,
            management=True,
        )
        response = client.get(
            "/v1/auth/verification-status",
            headers={"Authorization": f"Bearer {raw_key}"},
        )

    assert response.status_code == 403
    assert "+13059511381" not in response.text
    assert "alice@example.com" not in response.text
