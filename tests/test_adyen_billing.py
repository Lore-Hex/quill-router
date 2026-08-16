from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import adyen_billing
from trusted_router.services.adyen_billing import adyen_notification_signature
from trusted_router.storage import STORE
from trusted_router.typed_balance import live_credit_summary

HMAC_KEY = "44782DEF547AAA06C910C43932B1EB0C71FC68D9D0C057550C48EC2ACF6BA056"
REFERENCE_KEY = "trustedrouter-test-reference-key-32-bytes-minimum"
_HTTPX_CLIENT = httpx.Client


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "adyen_enabled": True,
        "adyen_api_key": "AQE_TEST_API_KEY",
        "adyen_client_key": "test_CLIENT_KEY",
        "adyen_hmac_key": HMAC_KEY,
        "adyen_reference_key": REFERENCE_KEY,
        "adyen_merchant_account": "TrustedRouterUS",
        "adyen_environment": "test",
        "sentry_dsn": None,
    }
    values.update(overrides)
    return Settings(**values)


def _http_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.Client]:
    def factory(*_args: Any, **_kwargs: Any) -> httpx.Client:
        return _HTTPX_CLIENT(transport=httpx.MockTransport(handler))

    return factory


def _workspace_id(client: TestClient) -> str:
    response = client.get(
        "/v1/workspaces", headers={"x-trustedrouter-user": "adyen@example.com"}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["data"][0]["id"])


def _merchant_reference(
    workspace_id: str, *, credit_cents: int = 500, charge_cents: int = 500
) -> str:
    return adyen_billing._new_checkout_reference(
        workspace_id=workspace_id,
        credit_amount_cents=credit_cents,
        charge_amount_cents=charge_cents,
        reference_key=REFERENCE_KEY,
    )


def _notification_item(
    workspace_id: str,
    *,
    event_code: str = "AUTHORISATION",
    success: str = "true",
    psp_reference: str = "PSP000000000001",
    merchant: str = "TrustedRouterUS",
    currency: str = "USD",
    value: int = 500,
    merchant_reference: str | None = None,
    hmac_key: str = HMAC_KEY,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "additionalData": {},
        "amount": {"currency": currency, "value": value},
        "eventCode": event_code,
        "merchantAccountCode": merchant,
        "merchantReference": merchant_reference or _merchant_reference(workspace_id),
        "originalReference": "",
        "pspReference": psp_reference,
        "success": success,
    }
    item["additionalData"]["hmacSignature"] = adyen_notification_signature(
        item, hmac_key
    )
    return item


def _webhook_payload(*items: dict[str, Any], live: bool = False) -> dict[str, Any]:
    return {
        "live": "true" if live else "false",
        "notificationItems": [
            {"NotificationRequestItem": item} for item in items
        ],
    }


def _credits(workspace_id: str) -> int:
    summary = live_credit_summary(workspace_id)
    assert summary is not None
    return summary["total_credits"]


def test_settings_keep_adyen_dark_and_fail_closed_when_enabled() -> None:
    dark = Settings(environment="test")
    assert dark.adyen_enabled is False
    assert dark.adyen_checkout_ready is False
    assert dark.adyen_webhook_ready is False

    with pytest.raises(ValidationError, match="TR_ADYEN_API_KEY"):
        Settings(environment="test", adyen_enabled=True)
    with pytest.raises(ValidationError, match="TR_ADYEN_REFERENCE_KEY"):
        _settings(adyen_reference_key=None)
    with pytest.raises(ValidationError, match="TR_ADYEN_LIVE_ENDPOINT_PREFIX"):
        _settings(adyen_environment="live")
    with pytest.raises(ValidationError, match="TR_ADYEN_ENVIRONMENT"):
        _settings(adyen_environment="sandbox")
    with pytest.raises(ValidationError, match="TR_ADYEN_HMAC_KEY"):
        _settings(adyen_hmac_key="not-hex")
    with pytest.raises(ValidationError, match="TR_ADYEN_HMAC_KEY"):
        _settings(adyen_hmac_key="ab" * 31)
    with pytest.raises(ValidationError, match="TR_ADYEN_REFERENCE_KEY"):
        _settings(adyen_reference_key="too-short")


def test_official_adyen_hmac_vector() -> None:
    item = {
        "additionalData": {},
        "amount": {"value": 1130, "currency": "EUR"},
        "pspReference": "7914073381342284",
        "originalReference": "",
        "merchantAccountCode": "TestMerchant",
        "merchantReference": "TestPayment-1407325143704",
        "eventCode": "AUTHORISATION",
        "success": "true",
    }
    assert (
        adyen_notification_signature(item, HMAC_KEY)
        == "coqCmt/IZ4E3CzPvMY8zTjQVL5hYJUiBRg8UU+iCWo0="
    )


def test_adyen_checkout_session_has_exact_money_and_no_balance_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"id": "CS_TEST_1", "sessionData": "opaque_session_data"},
        )

    monkeypatch.setattr(
        adyen_billing.httpx,
        "Client",
        _http_client_factory(handler),
    )
    app = create_app(
        _settings(adyen_card_fee_basis_points=300, adyen_card_fee_fixed_cents=30),
        init_observability=False,
    )
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        before = _credits(workspace_id)
        response = client.post(
            "/v1/billing/checkout",
            headers={"x-trustedrouter-user": "adyen@example.com"},
            json={"amount": "25.00", "payment_method": "adyen"},
        )
        after = _credits(workspace_id)

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["mode"] == "adyen"
    assert data["id"] == "CS_TEST_1"
    assert data["session_data"] == "opaque_session_data"
    assert data["client_key"] == "test_CLIENT_KEY"
    assert "AQE_TEST_API_KEY" not in response.text
    assert after == before

    request = captured["request"]
    payload = captured["payload"]
    assert str(request.url) == "https://checkout-test.adyen.com/v72/sessions"
    assert request.headers["x-api-key"] == "AQE_TEST_API_KEY"
    assert re.fullmatch(r"[0-9a-f-]{36}", request.headers["idempotency-key"])
    assert payload["merchantAccount"] == "TrustedRouterUS"
    assert "merchantOrderReference" not in payload
    assert payload["reference"].startswith(
        f"trc_{workspace_id.replace('-', '')}_1xg_"
    )
    assert len(payload["reference"]) <= 80
    assert payload["amount"] == {"currency": "USD", "value": 2609}
    assert [item["description"] for item in payload["lineItems"]] == [
        "TrustedRouter credits",
        "Payment processing fee",
    ]
    assert sum(item["amountIncludingTax"] for item in payload["lineItems"]) == 2609


def test_small_adyen_checkout_applies_card_fee_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "CS_FLOOR", "sessionData": "opaque"})

    monkeypatch.setattr(
        adyen_billing.httpx,
        "Client",
        _http_client_factory(handler),
    )
    app = create_app(
        _settings(adyen_card_fee_basis_points=300, adyen_card_fee_fixed_cents=30),
        init_observability=False,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/billing/checkout",
            headers={"x-trustedrouter-user": "adyen@example.com"},
            json={"amount": "3.00", "payment_method": "adyen"},
        )

    assert response.status_code == 201, response.text
    payload = captured["payload"]
    assert payload["amount"] == {"currency": "USD", "value": 380}
    assert [item["amountIncludingTax"] for item in payload["lineItems"]] == [300, 80]


def test_adyen_inactive_merchant_is_a_retryable_checkout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"errorCode": "901", "message": "Invalid Merchant Account"},
        )

    monkeypatch.setattr(
        adyen_billing.httpx, "Client", _http_client_factory(handler)
    )
    app = create_app(_settings(), init_observability=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/billing/checkout",
            headers={"x-trustedrouter-user": "adyen@example.com"},
            json={"amount": 5, "payment_method": "adyen"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["message"] == (
        "Adyen merchant account is not active"
    )


def test_adyen_checkout_cannot_be_requested_while_dark() -> None:
    app = create_app(Settings(environment="test"), init_observability=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/billing/checkout",
            headers={"x-trustedrouter-user": "adyen@example.com"},
            json={"amount": 5, "payment_method": "adyen"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Adyen checkout is not configured"


@pytest.mark.parametrize(
    ("handler", "expected_status"),
    [
        (lambda _request: httpx.Response(201, json={}), 502),
        (
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("offline", request=request)
            ),
            503,
        ),
    ],
)
def test_adyen_checkout_fails_closed_on_bad_or_unreachable_session_response(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    expected_status: int,
) -> None:
    monkeypatch.setattr(
        adyen_billing.httpx, "Client", _http_client_factory(handler)
    )
    app = create_app(_settings(), init_observability=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/billing/checkout",
            headers={"x-trustedrouter-user": "adyen@example.com"},
            json={"amount": 5, "payment_method": "adyen"},
        )
    assert response.status_code == expected_status


def test_adyen_authorisation_credits_exactly_once() -> None:
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        before = _credits(workspace_id)
        item = _notification_item(workspace_id)
        first = client.post("/v1/internal/adyen/webhook", json=_webhook_payload(item))
        second_item = _notification_item(
            workspace_id,
            psp_reference="PSP000000000002",
            merchant_reference=str(item["merchantReference"]),
        )
        second = client.post(
            "/v1/internal/adyen/webhook", json=_webhook_payload(second_item)
        )
        after = _credits(workspace_id)

    assert first.status_code == 200
    assert first.text == "[accepted]"
    assert second.status_code == 200
    assert after - before == 5_000_000


def test_adyen_authorisation_accrues_lifetime_topup_to_the_owner_exactly_once() -> None:
    # Every real purchase must accrue lifetime top-up, or an Adyen payer stays
    # funding-gated for phone verification. The signed reference carries no
    # initiator, so it lands on the workspace owner; the replayed notification
    # must not accrue twice.
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        owner_id = STORE.get_workspace(workspace_id).owner_user_id
        assert STORE.get_lifetime_topup_microdollars(owner_id) == 0
        item = _notification_item(workspace_id)
        client.post("/v1/internal/adyen/webhook", json=_webhook_payload(item))
        replay = _notification_item(
            workspace_id,
            psp_reference="PSP000000000002",
            merchant_reference=str(item["merchantReference"]),
        )
        client.post("/v1/internal/adyen/webhook", json=_webhook_payload(replay))

    assert STORE.get_lifetime_topup_microdollars(owner_id) == 5_000_000


def test_adyen_webhook_remains_available_when_checkout_is_dark_but_needs_hmac() -> None:
    missing_hmac = _settings(
        adyen_enabled=False,
        adyen_hmac_key=None,
    )
    app = create_app(missing_hmac, init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        response = client.post(
            "/v1/internal/adyen/webhook",
            json=_webhook_payload(_notification_item(workspace_id)),
        )
    assert response.status_code == 503
    assert "verification is not configured" in response.text


def test_adyen_signed_but_unissued_reference_cannot_credit() -> None:
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        before = _credits(workspace_id)
        issued = _merchant_reference(workspace_id)
        forged = f"{issued.rsplit('_', 1)[0]}_0000000000000000"
        item = _notification_item(workspace_id, merchant_reference=forged)
        response = client.post(
            "/v1/internal/adyen/webhook", json=_webhook_payload(item)
        )
        assert _credits(workspace_id) == before
    assert response.status_code == 400
    assert "checkout reference" in response.text


def test_adyen_failed_authorisation_and_adverse_event_never_mutate_balance() -> None:
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        before = _credits(workspace_id)
        failed = _notification_item(workspace_id, success="false")
        chargeback = _notification_item(
            workspace_id,
            event_code="CHARGEBACK",
            psp_reference="PSP000000000003",
        )
        assert client.post(
            "/v1/internal/adyen/webhook", json=_webhook_payload(failed)
        ).status_code == 200
        assert client.post(
            "/v1/internal/adyen/webhook", json=_webhook_payload(chargeback)
        ).status_code == 200
        assert _credits(workspace_id) == before


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        (lambda item: item.update(merchantAccountCode="WrongMerchant"), "merchant mismatch"),
        (lambda item: item["amount"].update(currency="EUR"), "currency mismatch"),
        (lambda item: item["amount"].update(value=501), "amount mismatch"),
    ],
)
def test_adyen_webhook_rejects_mismatched_payment_claims(
    mutation: Callable[[dict[str, Any]], None],
    expected_message: str,
) -> None:
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        before = _credits(workspace_id)
        item = _notification_item(workspace_id)
        mutation(item)
        item["additionalData"]["hmacSignature"] = adyen_notification_signature(
            item, HMAC_KEY
        )
        response = client.post(
            "/v1/internal/adyen/webhook", json=_webhook_payload(item)
        )
        assert _credits(workspace_id) == before
    assert response.status_code == 400
    assert expected_message in response.text


def test_adyen_webhook_rejects_live_test_mismatch() -> None:
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        response = client.post(
            "/v1/internal/adyen/webhook",
            json=_webhook_payload(_notification_item(workspace_id), live=True),
        )
    assert response.status_code == 400
    assert "environment mismatch" in response.text


def test_adyen_batch_is_preverified_before_any_credit() -> None:
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        before = _credits(workspace_id)
        valid = _notification_item(workspace_id)
        forged = _notification_item(
            workspace_id,
            psp_reference="PSP000000000004",
            merchant_reference=_merchant_reference(
                workspace_id, credit_cents=700, charge_cents=700
            ),
            value=700,
        )
        forged["additionalData"]["hmacSignature"] = "forged"
        response = client.post(
            "/v1/internal/adyen/webhook",
            json=_webhook_payload(valid, forged),
        )
        assert _credits(workspace_id) == before
    assert response.status_code == 400
    assert "signature" in response.text


def test_adyen_batch_is_fully_validated_before_any_credit() -> None:
    app = create_app(_settings(adyen_enabled=False), init_observability=False)
    with TestClient(app) as client:
        workspace_id = _workspace_id(client)
        before = _credits(workspace_id)
        valid = _notification_item(workspace_id)
        invalid_amount = _notification_item(
            workspace_id,
            psp_reference="PSP000000000005",
            merchant_reference=_merchant_reference(
                workspace_id, credit_cents=700, charge_cents=700
            ),
            value=701,
        )
        response = client.post(
            "/v1/internal/adyen/webhook",
            json=_webhook_payload(valid, invalid_amount),
        )
        assert _credits(workspace_id) == before
    assert response.status_code == 400
    assert "amount mismatch" in response.text


def test_adyen_console_is_hidden_until_ready_and_embeds_pinned_dropin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_app = create_app(Settings(environment="test"), init_observability=False)
    with TestClient(disabled_app) as disabled_client:
        user = STORE.ensure_user("console-adyen@example.com")
        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="email",
            label=user.email,
            ttl_seconds=3600,
            state="active",
        )
        disabled_client.cookies.set("tr_session", raw_token)
        page = disabled_client.get("/console/credits")
        assert page.status_code == 200
        assert '<option value="adyen">' not in page.text

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": "CS_CONSOLE", "sessionData": "console_session_data"},
        )

    monkeypatch.setattr(
        adyen_billing.httpx, "Client", _http_client_factory(handler)
    )
    enabled_app = create_app(_settings(), init_observability=False)
    with TestClient(enabled_app) as enabled_client:
        user = STORE.ensure_user("console-adyen@example.com")
        raw_token, _ = STORE.create_auth_session(
            user_id=user.id,
            provider="email",
            label=user.email,
            ttl_seconds=3600,
            state="active",
        )
        enabled_client.cookies.set("tr_session", raw_token)
        page = enabled_client.get("/console/credits")
        assert '<option value="adyen">Adyen</option>' in page.text
        checkout = enabled_client.post(
            "/console/credits/checkout",
            data={"amount": "25", "payment_method": "adyen"},
        )

    assert checkout.status_code == 200
    assert "checkoutshopper-test.cdn.adyen.com" in checkout.text
    assert adyen_billing.ADYEN_WEB_JS_SRI in checkout.text
    assert adyen_billing.ADYEN_WEB_CSS_SRI in checkout.text
    assert "console_session_data" in checkout.text
    assert "test_CLIENT_KEY" in checkout.text
    assert "AQE_TEST_API_KEY" not in checkout.text
    assert "Payment processing fee" in checkout.text
    assert "onPaymentFailed" in checkout.text
    assert "const { AdyenCheckout, Dropin }" in checkout.text
