from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.routable_payouts import (
    ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS,
    new_payout_id,
    normalize_routable_status,
    payout_idempotency_entity_id,
    payout_request_fingerprint,
    routable_amount,
    routable_company_external_id,
    routable_error_is_definitive_no_effect,
    routable_send_date,
)
from trusted_router.routes import payouts as payout_routes
from trusted_router.routes.internal import routable as webhook_routes
from trusted_router.services.routable_payouts import (
    RoutableAPIError,
    RoutableClient,
    invitation_url,
    valid_bank_payment_method,
    verify_routable_webhook,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import EarningsCashout, RoutablePayoutProfile, User

_CREDENTIALS = {
    "routable_api_token": "routable-" + "token",
    "routable_webhook_secret": "routable-" + "webhook-secret",
    "routable_company_id": "company-trustedrouter",
    "routable_team_member_id": "member-operator",
    "routable_withdraw_from_account_id": "account-operating",
}


class FakeRoutableClient:
    def __init__(self) -> None:
        self.company: dict[str, Any] | None = {
            "id": "company-creator",
            "status": "accepted",
        }
        self.payment_methods: list[dict[str, Any]] = [
            {
                "id": "bank-creator",
                "type": "bank",
                "is_archived": False,
                "is_valid": True,
                "verification_status": "verified",
            }
        ]
        self.payables: dict[str, dict[str, Any]] = {}
        self.payable_by_id: dict[str, dict[str, Any]] = {}
        self.create_company_calls: list[dict[str, Any]] = []
        self.invite_calls: list[tuple[str, str]] = []
        self.reinvite_calls: list[tuple[str, str]] = []
        self.create_payable_calls: list[dict[str, Any]] = []
        self.create_payable_error: RoutableAPIError | None = None
        self.retrieve_payable_error: RoutableAPIError | None = None

    async def find_company(self, _external_id: str) -> dict[str, Any] | None:
        return self.company

    async def retrieve_company(self, _company_id: str) -> dict[str, Any]:
        assert self.company is not None
        return self.company

    async def create_company(self, **kwargs: Any) -> dict[str, Any]:
        self.create_company_calls.append(kwargs)
        self.company = {"id": "company-new", "status": "added"}
        return self.company

    async def invite_company(
        self,
        company_id: str,
        *,
        confirmation_redirect_url: str,
    ) -> dict[str, Any]:
        self.invite_calls.append((company_id, confirmation_redirect_url))
        return self._invitation()

    async def reinvite_company(
        self,
        company_id: str,
        *,
        confirmation_redirect_url: str,
    ) -> dict[str, Any]:
        self.reinvite_calls.append((company_id, confirmation_redirect_url))
        return self._invitation()

    @staticmethod
    def _invitation() -> dict[str, Any]:
        return {
            "contacts": {
                "results": [
                    {
                        "links": {
                            "invitation_url": "https://app.routable.com/invite/signed"
                        }
                    }
                ]
            }
        }

    async def list_payment_methods(self, _company_id: str) -> list[dict[str, Any]]:
        return self.payment_methods

    async def find_payable(self, external_id: str) -> dict[str, Any] | None:
        return self.payables.get(external_id)

    async def create_payable(self, **kwargs: Any) -> dict[str, Any]:
        self.create_payable_calls.append(kwargs)
        if self.create_payable_error is not None:
            raise self.create_payable_error
        payable = {
            "id": f"payable-{len(self.create_payable_calls)}",
            "external_id": kwargs["external_id"],
            "status": "ready_to_send",
        }
        self.payables[str(kwargs["external_id"])] = payable
        self.payable_by_id[str(payable["id"])] = payable
        return payable

    async def retrieve_payable(self, payable_id: str) -> dict[str, Any]:
        if self.retrieve_payable_error is not None:
            raise self.retrieve_payable_error
        return self.payable_by_id[payable_id]


def _settings(base: Settings, *, enabled: bool = True) -> Settings:
    return base.model_copy(
        update={
            **_CREDENTIALS,
            "routable_enabled": enabled,
            "routable_api_base_url": "https://api.routable.test",
        }
    )


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, init_observability=False))


def _sign_in(
    client: TestClient,
    *,
    email: str = "creator@example.com",
    identity_verified: bool = True,
    email_verified: bool = True,
) -> User:
    user = STORE.ensure_user(email)
    if email_verified:
        user = STORE.mark_user_email_verified(user.id)
    if identity_verified:
        user = STORE.set_user_identity_status(
            user.id,
            status="approved",
            verified_name="Ada Creator",
        )
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label=email,
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    return user


def _seed_profile(user: User) -> RoutablePayoutProfile:
    return STORE.upsert_routable_payout_profile(
        RoutablePayoutProfile(
            user_id=user.id,
            routable_company_id="company-creator",
            company_status="accepted",
            payment_method_id="bank-creator",
            payment_method_type="bank",
        )
    )


def _seed_cashout(
    user: User,
    *,
    amount: int = ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS,
    payable_id: str = "payable-webhook",
) -> EarningsCashout:
    assert STORE.credit_user_earnings(user.id, amount, f"fund:{payable_id}")
    payout_id = new_payout_id()
    cashout = EarningsCashout(
        id=payout_id,
        user_id=user.id,
        amount_microdollars=amount,
        state="reserved",
        balance_status="reserved",
        idempotency_fingerprint=payout_request_fingerprint(
            user_id=user.id,
            amount_microdollars=amount,
            routable_company_id="company-creator",
            payment_method_id="bank-creator",
        ),
        routable_idempotency_key=f"route-{payout_id}",
        external_id=f"external-{payout_id}",
        routable_company_id="company-creator",
        payment_method_id="bank-creator",
    )
    outcome, _ = STORE.reserve_earnings_cashout(
        cashout,
        idempotency_entity_id=payout_idempotency_entity_id(user.id, payout_id),
    )
    assert outcome == "accepted"
    marked = STORE.mark_earnings_cashout(
        user.id,
        payout_id,
        state="pending",
        routable_payable_id=payable_id,
        routable_status="pending",
    )
    assert marked is not None
    return marked


def _signed_webhook(
    settings: Settings,
    *,
    object_id: str,
    now: datetime | None = None,
    company_id: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    timestamp = (now or datetime.now(UTC)).replace(microsecond=0)
    body = json.dumps(
        {
            "event_name": "payable.status_changed",
            "event_resource": "payable",
            "company_id": company_id or str(settings.routable_company_id),
            "object_id": object_id,
        },
        separators=(",", ":"),
    ).encode()
    timestamp_text = timestamp.isoformat().replace("+00:00", "Z")
    signature = hmac.new(
        str(settings.routable_webhook_secret).encode(),
        timestamp_text.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "Routable-Signature-Timestamp": timestamp_text,
        "Routable-Signature": signature,
    }


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeRoutableClient,
) -> None:
    factory = lambda *_args, **_kwargs: fake  # noqa: E731
    monkeypatch.setattr(payout_routes, "RoutableClient", factory)
    monkeypatch.setattr(webhook_routes, "RoutableClient", factory)


def test_routable_config_is_dark_by_default_and_fails_closed() -> None:
    dark = Settings(environment="test", routable_enabled=False)
    assert dark.routable_configured is False
    assert dark.routable_credentials_configured is False

    with pytest.raises(ValidationError, match="TR_ROUTABLE_API_TOKEN"):
        Settings(
            environment="test",
            routable_enabled=True,
            routable_api_token=None,
            routable_webhook_secret=None,
            routable_company_id=None,
            routable_team_member_id=None,
            routable_withdraw_from_account_id=None,
        )
    with pytest.raises(ValidationError, match="must use https"):
        Settings(
            environment="test",
            routable_enabled=False,
            routable_api_base_url="http://routable.invalid",
        )


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (100_000_000, "100.00"),
        (100_010_000, "100.01"),
        (9_999_990_000, "9999.99"),
    ],
)
def test_routable_amount_is_exact_integer_money(amount: int, expected: str) -> None:
    assert routable_amount(amount) == expected


@pytest.mark.parametrize("amount", [0, -10_000, 100_000_001])
def test_routable_amount_rejects_nonpositive_or_subcent_values(amount: int) -> None:
    with pytest.raises(ValueError):
        routable_amount(amount)


def test_routable_send_date_is_frozen_in_pacific_time() -> None:
    assert routable_send_date("2026-09-02T06:59:59Z") == "2026-09-01"
    assert routable_send_date("2026-09-02T07:00:00Z") == "2026-09-02"


def test_routable_status_and_http_effect_classification_are_conservative() -> None:
    assert normalize_routable_status(" COMPLETED ") == "completed"
    assert normalize_routable_status("invented") is None
    assert routable_error_is_definitive_no_effect(400)
    assert routable_error_is_definitive_no_effect(422)
    assert not routable_error_is_definitive_no_effect(409)
    assert not routable_error_is_definitive_no_effect(429)
    assert not routable_error_is_definitive_no_effect(500)
    assert not routable_error_is_definitive_no_effect(None)


@pytest.mark.asyncio
async def test_routable_client_sends_exact_company_invite_method_and_payable_shapes(
    test_settings: Settings,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/payment-methods"):
            return httpx.Response(200, json={"results": [{"id": "bank-1"}]})
        if request.url.path == "/v1/payables" and request.method == "GET":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "created"})

    settings = _settings(test_settings)
    client = RoutableClient(settings, transport=httpx.MockTransport(handler))
    await client.create_company(
        external_id="tr-user-safe",
        recipient_type="personal",
        country_code="US",
        first_name="Ada",
        last_name="Creator",
        email="creator@example.com",
    )
    await client.invite_company(
        "company-1",
        confirmation_redirect_url="https://trustedrouter.com/console/earnings",
    )
    await client.reinvite_company(
        "company-1",
        confirmation_redirect_url="https://trustedrouter.com/console/earnings",
    )
    methods = await client.list_payment_methods("company-1")
    assert methods == [{"id": "bank-1"}]
    assert await client.find_payable("external-1") is None
    await client.create_payable(
        amount_microdollars=100_010_000,
        external_id="external-1",
        company_id="company-1",
        payment_method_id="bank-1",
        idempotency_key="idempotency-1",
        send_on="2026-09-02",
    )

    assert all(
        request.headers["authorization"] == f"Bearer {_CREDENTIALS['routable_api_token']}"
        for request in requests
    )
    company_body = json.loads(requests[0].content)
    assert company_body == {
        "acting_team_member": "member-operator",
        "collect_tax_form": True,
        "contacts": [
            {
                "email": "creator@example.com",
                "first_name": "Ada",
                "last_name": "Creator",
                "allow_for_multiple_companies": False,
                "default_contact_for_company_management": "actionable",
                "default_contact_for_payable_and_receivable": "none",
            }
        ],
        "country_code": "US",
        "external_id": "tr-user-safe",
        "is_customer": False,
        "is_vendor": True,
        "type": "personal",
        "display_name": "Ada Creator",
    }
    invite_body = json.loads(requests[1].content)
    assert invite_body["get_links"] is True
    assert invite_body["send_invite_email"] is False
    assert invite_body["confirmation_redirect_url"].startswith("https://")
    reinvite_request = requests[2]
    reinvite_body = json.loads(reinvite_request.content)
    assert reinvite_request.method == "PATCH"
    assert reinvite_body["request_payment_method"] is True
    assert reinvite_body["request_tax_form"] is True
    assert reinvite_body["get_links"] is True
    assert reinvite_body["send_invite_email"] is False
    assert dict(requests[3].url.params) == {
        "archival_status": "not_archived",
        "is_valid": "true",
    }
    payable_request = requests[-1]
    payable_body = json.loads(payable_request.content)
    assert payable_request.headers["idempotency-key"] == "idempotency-1"
    assert payable_body["amount"] == "100.01"
    assert payable_body["line_items"][0]["unit_price"] == "100.01"
    assert payable_body["send_on"] == "2026-09-02"
    assert payable_body["delivery_method"] == "ach_standard"


@pytest.mark.asyncio
async def test_routable_client_errors_do_not_disclose_token_or_response_body(
    test_settings: Settings,
) -> None:
    secret_body = "private-provider-detail-" + "x" * 20

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"Request-ID": "req-routable"},
            json={"title": "Provider exploded", "detail": secret_body},
        )

    client = RoutableClient(
        _settings(test_settings),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RoutableAPIError) as caught:
        await client.find_company("external")
    rendered = str(caught.value)
    assert caught.value.status_code == 500
    assert caught.value.request_id == "req-routable"
    assert caught.value.code == "provider_exploded"
    assert _CREDENTIALS["routable_api_token"] not in rendered
    assert secret_body not in rendered


@pytest.mark.asyncio
async def test_routable_client_rejects_invalid_success_payload(
    test_settings: Settings,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"not-json")
    )
    client = RoutableClient(_settings(test_settings), transport=transport)
    with pytest.raises(RoutableAPIError, match="invalid_json"):
        await client.find_company("external")


def test_routable_helpers_accept_only_safe_urls_and_valid_bank_methods() -> None:
    assert invitation_url({"external_flow_url": "https://app.routable.com/invite"})
    assert invitation_url({"external_flow_url": "javascript:alert(1)"}) is None
    assert invitation_url({"external_flow_url": "http://app.routable.com/invite"}) is None
    assert invitation_url({"external_flow_url": "https://routable.com.evil.test/invite"}) is None
    assert invitation_url({"external_flow_url": "https://user@app.routable.com/invite"}) is None
    assert invitation_url(
        {
            "contacts": {
                "results": [
                    {"links": {"invitation_url": "https://app.routable.com/contact"}}
                ]
            }
        }
    ) == "https://app.routable.com/contact"
    assert valid_bank_payment_method(
        {"type": "bank", "is_valid": True, "verification_status": "verified"}
    )
    assert not valid_bank_payment_method({"type": "card", "is_valid": True})
    assert not valid_bank_payment_method({"type": "bank", "is_archived": True})
    assert not valid_bank_payment_method(
        {"type": "bank", "verification_status": "unverified"}
    )


def test_webhook_signature_rejects_tampering_stale_future_and_wrong_company(
    test_settings: Settings,
) -> None:
    settings = _settings(test_settings)
    now = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
    body, headers = _signed_webhook(settings, object_id="payable-1", now=now)
    assert verify_routable_webhook(
        raw_body=body,
        headers=headers,
        settings=settings,
        now=now,
    ) == json.loads(body)
    assert verify_routable_webhook(
        raw_body=body + b" ",
        headers=headers,
        settings=settings,
        now=now,
    ) is None
    for offset in (timedelta(seconds=-301), timedelta(seconds=1)):
        shifted_body, shifted_headers = _signed_webhook(
            settings,
            object_id="payable-1",
            now=now + offset,
        )
        assert verify_routable_webhook(
            raw_body=shifted_body,
            headers=shifted_headers,
            settings=settings,
            now=now,
        ) is None
    wrong_body, wrong_headers = _signed_webhook(
        settings,
        object_id="payable-1",
        now=now,
        company_id="other-company",
    )
    assert verify_routable_webhook(
        raw_body=wrong_body,
        headers=wrong_headers,
        settings=settings,
        now=now,
    ) is None


def test_payout_status_is_dark_and_management_requires_browser_session(
    test_settings: Settings,
) -> None:
    client = _client(_settings(test_settings, enabled=False))
    unauthenticated = client.get("/v1/payouts")
    assert unauthenticated.status_code == 403

    user = _sign_in(client)
    STORE.credit_user_earnings(user.id, 125_000_000, "earnings-dark")
    status = client.get("/v1/payouts")
    assert status.status_code == 200
    assert status.json()["payouts_enabled"] is False
    assert status.json()["available_microdollars"] == 125_000_000
    disabled = client.post("/v1/payouts/onboarding", json={})
    assert disabled.status_code == 503
    assert disabled.headers["retry-after"] == "86400"


@pytest.mark.parametrize(
    ("identity_verified", "email_verified", "message"),
    [
        (False, True, "identity verification"),
        (True, False, "verified email"),
    ],
)
def test_payout_onboarding_requires_identity_and_email(
    test_settings: Settings,
    identity_verified: bool,
    email_verified: bool,
    message: str,
) -> None:
    client = _client(_settings(test_settings))
    _sign_in(
        client,
        email=f"{identity_verified}-{email_verified}@example.com",
        identity_verified=identity_verified,
        email_verified=email_verified,
    )
    response = client.post("/v1/payouts/onboarding", json={})
    assert response.status_code == 403
    assert message in response.json()["error"]["message"].lower()


def test_onboarding_creates_vendor_returns_hosted_url_and_redacts_ids(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(test_settings)
    client = _client(settings)
    user = _sign_in(client)
    fake = FakeRoutableClient()
    fake.company = None
    fake.payment_methods = []
    _patch_client(monkeypatch, fake)

    response = client.post(
        "/v1/payouts/onboarding",
        json={"recipient_type": "personal", "country_code": "US"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["onboarding_url"] == "https://app.routable.com/invite/signed"
    assert "routable_company_id" not in json.dumps(payload)
    assert "payment_method_id" not in json.dumps(payload)
    assert fake.create_company_calls[0]["external_id"] == routable_company_external_id(
        user.id
    )
    assert fake.create_company_calls[0]["email"] == user.email
    assert fake.invite_calls == [
        (
            "company-new",
            "https://trustedrouter.com/console/earnings?routable=return",
        )
    ]


def test_onboarding_skips_invite_for_already_accepted_vendor(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_settings(test_settings))
    _sign_in(client)
    fake = FakeRoutableClient()
    _patch_client(monkeypatch, fake)
    response = client.post("/v1/payouts/onboarding", json={})
    assert response.status_code == 200
    assert response.json()["onboarding_url"] is None
    assert response.json()["data"]["payment_method_ready"] is True
    assert fake.invite_calls == []
    assert fake.reinvite_calls == []


@pytest.mark.parametrize("company_status", ["invited", "accepted"])
def test_onboarding_reinvites_when_existing_vendor_still_needs_bank_details(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    company_status: str,
) -> None:
    client = _client(_settings(test_settings))
    _sign_in(client, email=f"reinvite-{company_status}@example.com")
    fake = FakeRoutableClient()
    assert fake.company is not None
    fake.company["status"] = company_status
    fake.payment_methods = []
    _patch_client(monkeypatch, fake)

    response = client.post("/v1/payouts/onboarding", json={})

    assert response.status_code == 200
    assert response.json()["onboarding_url"].startswith("https://app.routable.com/")
    assert fake.invite_calls == []
    assert fake.reinvite_calls == [
        (
            "company-creator",
            "https://trustedrouter.com/console/earnings?routable=return",
        )
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, "Invalid payout amount"),
        ({"amount": "99.99"}, "at least $100.00"),
        ({"amount": "100.000001"}, "whole cents"),
        ({"amount": "100.00", "extra": True}, "Unknown payout field"),
    ],
)
def test_payout_request_rejects_invalid_money_without_reserving(
    test_settings: Settings,
    body: dict[str, object],
    expected: str,
) -> None:
    client = _client(_settings(test_settings))
    user = _sign_in(client, email=f"money-{hash(str(body))}@example.com")
    _seed_profile(user)
    assert STORE.credit_user_earnings(user.id, 200_000_000, "money-validation")
    response = client.post(
        "/v1/payouts",
        headers={"Idempotency-Key": "money-validation"},
        json=body,
    )
    assert response.status_code == 400
    assert expected in response.json()["error"]["message"]
    assert STORE.earnings_summary(user.id)["available"] == 200_000_000


def test_payout_requires_bounded_idempotency_key(
    test_settings: Settings,
) -> None:
    client = _client(_settings(test_settings))
    user = _sign_in(client)
    _seed_profile(user)
    assert STORE.credit_user_earnings(user.id, 200_000_000, "fund-idempotency")
    for key in (None, "x" * 129):
        headers = {} if key is None else {"Idempotency-Key": key}
        response = client.post("/v1/payouts", headers=headers, json={"amount": "100"})
        assert response.status_code == 400
    assert STORE.earnings_summary(user.id)["available"] == 200_000_000


def test_payout_reserves_once_and_reuses_provider_payable_on_retry(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_settings(test_settings))
    user = _sign_in(client)
    _seed_profile(user)
    fake = FakeRoutableClient()
    _patch_client(monkeypatch, fake)
    assert STORE.credit_user_earnings(user.id, 200_000_000, "fund-success")

    first = client.post(
        "/v1/payouts",
        headers={"Idempotency-Key": "same-request"},
        json={"amount": "100.01"},
    )
    second = client.post(
        "/v1/payouts",
        headers={"Idempotency-Key": "same-request"},
        json={"amount": "100.01"},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert len(fake.create_payable_calls) == 1
    assert fake.create_payable_calls[0]["amount_microdollars"] == 100_010_000
    assert STORE.earnings_summary(user.id)["available"] == 99_990_000
    rendered = json.dumps(first.json())
    assert "company-creator" not in rendered
    assert "bank-creator" not in rendered
    assert "payable-1" not in rendered


def test_idempotency_key_with_different_amount_is_rejected_without_second_hold(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_settings(test_settings))
    user = _sign_in(client)
    _seed_profile(user)
    fake = FakeRoutableClient()
    _patch_client(monkeypatch, fake)
    assert STORE.credit_user_earnings(user.id, 300_000_000, "fund-conflict")
    headers = {"Idempotency-Key": "conflict-request"}
    assert client.post("/v1/payouts", headers=headers, json={"amount": "100"}).status_code == 201
    conflict = client.post("/v1/payouts", headers=headers, json={"amount": "101"})
    assert conflict.status_code == 409
    assert STORE.earnings_summary(user.id)["available"] == 200_000_000
    assert len(fake.create_payable_calls) == 1


@pytest.mark.parametrize(
    ("status_code", "expected_state", "expected_available"),
    [
        (400, "rejected", 100_000_000),
        (409, "submission_unknown", 0),
        (429, "submission_unknown", 0),
        (500, "submission_unknown", 0),
    ],
)
def test_submission_failure_releases_only_when_provider_proves_no_effect(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_state: str,
    expected_available: int,
) -> None:
    client = _client(_settings(test_settings))
    user = _sign_in(client, email=f"failure-{status_code}@example.com")
    _seed_profile(user)
    fake = FakeRoutableClient()
    fake.create_payable_error = RoutableAPIError(
        "provider_rejected",
        status_code=status_code,
    )
    _patch_client(monkeypatch, fake)
    assert STORE.credit_user_earnings(user.id, 100_000_000, f"fund-{status_code}")
    response = client.post(
        "/v1/payouts",
        headers={"Idempotency-Key": f"failure-{status_code}"},
        json={"amount": "100"},
    )
    expected_http = 201 if expected_state == "rejected" else 202
    assert response.status_code == expected_http
    assert response.json()["data"]["state"] == expected_state
    assert STORE.earnings_summary(user.id)["available"] == expected_available


def test_ambiguous_submission_can_be_retried_by_payout_id_without_second_hold(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(_settings(test_settings))
    user = _sign_in(client, email="retry-payout@example.com")
    _seed_profile(user)
    fake = FakeRoutableClient()
    fake.create_payable_error = RoutableAPIError("transport_error")
    _patch_client(monkeypatch, fake)
    assert STORE.credit_user_earnings(user.id, 100_000_000, "fund-retry")

    first = client.post(
        "/v1/payouts",
        headers={"Idempotency-Key": "ambiguous-retry"},
        json={"amount": "100"},
    )
    assert first.status_code == 202
    payout_id = first.json()["data"]["id"]
    assert STORE.earnings_summary(user.id)["available"] == 0

    fake.create_payable_error = None
    retried = client.post(f"/v1/payouts/{payout_id}/retry")

    assert retried.status_code == 200
    assert retried.json()["data"]["state"] == "ready_to_send"
    assert len(fake.create_payable_calls) == 2
    assert len({call["idempotency_key"] for call in fake.create_payable_calls}) == 1
    assert STORE.earnings_summary(user.id)["available"] == 0


def test_payout_retry_is_owner_scoped(
    test_settings: Settings,
) -> None:
    client = _client(_settings(test_settings))
    alice = _sign_in(client, email="alice-retry@example.com")
    cashout = _seed_cashout(alice, payable_id="payable-retry-alice")
    client.cookies.clear()
    _sign_in(client, email="bob-retry@example.com")

    assert client.post(f"/v1/payouts/{cashout.id}/retry").status_code == 404


def test_payout_list_and_get_are_user_scoped(
    test_settings: Settings,
) -> None:
    client = _client(_settings(test_settings))
    alice = _sign_in(client, email="alice-payout@example.com")
    alice_cashout = _seed_cashout(alice, payable_id="payable-alice")
    client.cookies.clear()
    _sign_in(client, email="bob-payout@example.com")
    assert client.get("/v1/payouts").json()["data"] == []
    assert client.get(f"/v1/payouts/{alice_cashout.id}").status_code == 404


@pytest.mark.parametrize(
    ("routable_status", "balance_status", "available"),
    [
        ("completed", "paid", 0),
        ("failed", "reserved", 0),
        ("issue", "reserved", 0),
        ("canceled", "released", ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS),
    ],
)
def test_signed_webhook_reconciles_and_replay_is_balance_idempotent(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    routable_status: str,
    balance_status: str,
    available: int,
) -> None:
    settings = _settings(test_settings)
    client = _client(settings)
    user = _sign_in(client, email=f"webhook-{routable_status}@example.com")
    cashout = _seed_cashout(user)
    fake = FakeRoutableClient()
    fake.payable_by_id["payable-webhook"] = {
        "id": "payable-webhook",
        "external_id": cashout.external_id,
        "status": routable_status,
    }
    _patch_client(monkeypatch, fake)
    body, headers = _signed_webhook(settings, object_id="payable-webhook")
    first = client.post("/v1/internal/routable/webhook", content=body, headers=headers)
    second = client.post("/v1/internal/routable/webhook", content=body, headers=headers)
    assert first.status_code == second.status_code == 200
    stored = STORE.get_earnings_cashout(user.id, cashout.id)
    assert stored is not None
    assert stored.balance_status == balance_status
    assert STORE.earnings_summary(user.id)["available"] == available


def test_webhook_rejects_invalid_signature_and_retries_provider_failure(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(test_settings)
    client = _client(settings)
    fake = FakeRoutableClient()
    fake.retrieve_payable_error = RoutableAPIError("transport_error")
    _patch_client(monkeypatch, fake)
    body, headers = _signed_webhook(settings, object_id="payable-missing")
    invalid_headers = {**headers, "Routable-Signature": "bad"}
    assert client.post(
        "/v1/internal/routable/webhook",
        content=body,
        headers=invalid_headers,
    ).status_code == 401
    retry = client.post("/v1/internal/routable/webhook", content=body, headers=headers)
    assert retry.status_code == 503
    assert retry.headers["retry-after"] == "1"


def test_webhook_still_reconciles_when_new_cashouts_are_disabled(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(test_settings, enabled=False)
    client = _client(settings)
    user = _sign_in(client, email="disabled-webhook@example.com")
    cashout = _seed_cashout(user)
    fake = FakeRoutableClient()
    fake.payable_by_id["payable-webhook"] = {
        "id": "payable-webhook",
        "external_id": cashout.external_id,
        "status": "completed",
    }
    _patch_client(monkeypatch, fake)
    body, headers = _signed_webhook(settings, object_id="payable-webhook")
    response = client.post(
        "/v1/internal/routable/webhook",
        content=body,
        headers=headers,
    )
    assert response.status_code == 200
    stored = STORE.get_earnings_cashout(user.id, cashout.id)
    assert stored is not None and stored.balance_status == "paid"


def test_minimum_cashout_constant_is_exactly_one_hundred_dollars() -> None:
    assert ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS == 100 * MICRODOLLARS_PER_DOLLAR


def test_console_reuses_cashout_idempotency_key_until_success() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "trusted_router"
        / "static"
        / "earnings.js"
    ).read_text()
    assert "sessionStorage.getItem(payoutIdempotencyStorageKey)" in source
    assert '"Idempotency-Key": currentPayoutIdempotencyKey()' in source
    response_check = source.index("if (response.status === 202)")
    clear_call = source.index("clearPayoutIdempotencyKey();", response_check)
    assert response_check > source.index("const payload = await parseResponse(response);")
    assert clear_call > response_check
    assert '"Idempotency-Key": crypto.randomUUID()' not in source


def test_custom_model_public_docs_cover_namespaces_and_creator_cashouts() -> None:
    client = _client(Settings(environment="test"))
    page = client.get("/docs/custom-models")
    assert page.status_code == 200
    assert "tr-custom-model/ada-contract-reviewer" in page.text
    assert "70% of collected markup" in page.text
    assert "$100" in page.text
    assert "Routable" in page.text

    docs_index = client.get("/docs")
    assert docs_index.status_code == 200
    assert 'href="/docs/custom-models"' in docs_index.text
