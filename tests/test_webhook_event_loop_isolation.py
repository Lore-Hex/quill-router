from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.paypal_billing import verify_paypal_webhook_signature


def _loop_thread_client(settings: Settings) -> tuple[TestClient, str]:
    app = create_app(settings, init_observability=False)

    @app.get("/__test/event-loop-thread", include_in_schema=False)
    async def event_loop_thread() -> dict[str, int]:
        return {"thread_id": threading.get_ident()}

    client = TestClient(app)
    loop_thread = str(client.get("/__test/event-loop-thread").json()["thread_id"])
    return client, loop_thread


def test_deployed_paypal_webhook_fails_closed_without_webhook_id() -> None:
    settings = Settings(
        environment="test",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
    )
    settings.environment = "canary"

    with pytest.raises(HTTPException) as captured:
        verify_paypal_webhook_signature(headers={}, event={}, settings=settings)

    assert captured.value.status_code == 400


def test_paypal_webhook_verification_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        environment="test",
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id="WEBHOOKID",
    )
    client, loop_thread = _loop_thread_client(settings)
    verification_threads: list[str] = []

    def verify(**_kwargs: object) -> None:
        verification_threads.append(str(threading.get_ident()))

    monkeypatch.setattr(
        "trusted_router.routes.internal.paypal.verify_paypal_webhook_signature",
        verify,
    )

    response = client.post(
        "/v1/internal/paypal/webhook",
        json={"id": "WH-THREAD", "event_type": "IGNORED"},
    )

    assert response.status_code == 200
    assert verification_threads
    assert verification_threads[0] != loop_thread


def test_sns_certificate_verification_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, loop_thread = _loop_thread_client(Settings(environment="test"))
    verification_threads: list[str] = []

    def verify(_envelope: dict[str, object]) -> None:
        verification_threads.append(str(threading.get_ident()))

    monkeypatch.setattr(
        "trusted_router.routes.ses_notifications.verify_sns_message",
        verify,
    )

    response = client.post(
        "/v1/internal/ses/notifications",
        json={
            "Type": "Notification",
            "MessageId": "thread-check",
            "Message": "not-json-so-no-store-work-runs",
        },
    )

    assert response.status_code == 200
    assert verification_threads
    assert verification_threads[0] != loop_thread


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "paypal-transmission-id": "transmission",
            "paypal-transmission-time": "2026-08-19T00:00:00Z",
            "paypal-cert-url": "https://attacker.example/cert.pem",
            "paypal-auth-algo": "SHA256withRSA",
            "paypal-transmission-sig": "signature",
        },
        {
            "paypal-transmission-id": "transmission",
            "paypal-transmission-time": "2026-08-19T00:00:00Z",
            "paypal-cert-url": "https://api.paypal.com/cert",
            "paypal-auth-algo": "not-an-algorithm",
            "paypal-transmission-sig": "signature",
        },
    ],
)
def test_paypal_forged_headers_are_rejected_before_outbound_verification(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    outbound_calls = 0

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal outbound_calls
        outbound_calls += 1
        raise AssertionError("forged webhook reached PayPal")

    monkeypatch.setattr("trusted_router.services.paypal_billing._paypal_post", forbidden)
    settings = Settings(
        environment="test",
        paypal_enabled=True,
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id="WEBHOOKID",
    )

    with pytest.raises(HTTPException) as raised:
        verify_paypal_webhook_signature(headers=headers, event={"id": "WH"}, settings=settings)

    assert raised.value.status_code == 400
    assert outbound_calls == 0


def test_paypal_webhook_postback_has_a_process_wide_cost_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trusted_router.services.paypal_billing as paypal

    outbound_calls = 0

    def accepted(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal outbound_calls
        outbound_calls += 1
        return {"verification_status": "SUCCESS"}

    monkeypatch.setattr(paypal, "_paypal_post", accepted)
    paypal._PAYPAL_WEBHOOK_VERIFY_LIMITER.reset()
    settings = Settings(
        environment="test",
        paypal_enabled=True,
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id="WEBHOOKID",
    )
    headers = {
        "paypal-transmission-id": "transmission-id",
        "paypal-transmission-time": "2026-08-19T00:00:00Z",
        "paypal-cert-url": "https://api-m.paypal.com/v1/notifications/certs/CERT-1",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "signature",
    }
    try:
        for index in range(paypal._PAYPAL_WEBHOOK_VERIFY_MAX_PER_MINUTE):
            verify_paypal_webhook_signature(
                headers=headers,
                event={"id": f"WH-{index}"},
                settings=settings,
            )

        with pytest.raises(HTTPException) as raised:
            verify_paypal_webhook_signature(
                headers=headers,
                event={"id": "WH-OVER-LIMIT"},
                settings=settings,
            )
        assert raised.value.status_code == 429
        assert raised.value.headers is not None
        assert "Retry-After" in raised.value.headers
        assert outbound_calls == paypal._PAYPAL_WEBHOOK_VERIFY_MAX_PER_MINUTE
    finally:
        paypal._PAYPAL_WEBHOOK_VERIFY_LIMITER.reset()


def test_paypal_webhook_concurrency_ceiling_refuses_before_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trusted_router.services.paypal_billing as paypal

    outbound_calls = 0

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal outbound_calls
        outbound_calls += 1
        raise AssertionError("capacity-denied webhook reached PayPal")

    monkeypatch.setattr(paypal, "_paypal_post", forbidden)
    paypal._PAYPAL_WEBHOOK_VERIFY_LIMITER.reset()
    settings = Settings(
        environment="test",
        paypal_enabled=True,
        paypal_client_id="paypal-client",
        paypal_client_secret="paypal-secret",  # noqa: S106
        paypal_webhook_id="WEBHOOKID",
    )
    headers = {
        "paypal-transmission-id": "transmission-id",
        "paypal-transmission-time": "2026-08-19T00:00:00Z",
        "paypal-cert-url": "https://api-m.paypal.com/v1/notifications/certs/CERT-1",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "signature",
    }
    acquired = 0
    try:
        for _ in range(paypal._PAYPAL_WEBHOOK_VERIFY_MAX_CONCURRENT):
            assert paypal._PAYPAL_WEBHOOK_VERIFY_SLOTS.acquire(blocking=False)
            acquired += 1
        with pytest.raises(HTTPException) as raised:
            verify_paypal_webhook_signature(headers=headers, event={"id": "WH"}, settings=settings)
        assert raised.value.status_code == 429
        assert outbound_calls == 0
    finally:
        for _ in range(acquired):
            paypal._PAYPAL_WEBHOOK_VERIFY_SLOTS.release()
        paypal._PAYPAL_WEBHOOK_VERIFY_LIMITER.reset()


def test_sns_signature_concurrency_ceiling_refuses_before_fetch() -> None:
    import trusted_router.sns_verify as sns_verify

    fetch_calls = 0

    def forbidden(_url: str) -> bytes:
        nonlocal fetch_calls
        fetch_calls += 1
        raise AssertionError("capacity-denied SNS message fetched a certificate")

    envelope = {
        "Type": "Notification",
        "MessageId": "capacity",
        "TopicArn": "arn:aws:sns:us-east-1:123:ses-feedback",
        "Message": "{}",
        "Timestamp": "2026-08-19T00:00:00Z",
        "SignatureVersion": "2",
        "Signature": "not-used",
        "SigningCertURL": (
            "https://sns.us-east-1.amazonaws.com/"
            "SimpleNotificationService-capacity.pem"
        ),
    }
    acquired = 0
    try:
        for _ in range(sns_verify._SIGNATURE_VERIFY_MAX_CONCURRENT):
            assert sns_verify._SIGNATURE_VERIFY_SLOTS.acquire(blocking=False)
            acquired += 1
        with pytest.raises(sns_verify.SnsVerificationError, match="capacity"):
            sns_verify.verify_sns_message(envelope, cert_fetcher=forbidden)
        assert fetch_calls == 0
    finally:
        for _ in range(acquired):
            sns_verify._SIGNATURE_VERIFY_SLOTS.release()


def test_sns_subscription_confirmation_concurrency_refuses_before_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trusted_router.routes.ses_notifications as ses_notifications

    client, _loop_thread = _loop_thread_client(Settings(environment="test"))
    outbound_calls = 0

    def verified(_envelope: dict[str, object]) -> None:
        return None

    def forbidden(_url: str) -> None:
        nonlocal outbound_calls
        outbound_calls += 1
        raise AssertionError("capacity-denied confirmation reached SNS")

    monkeypatch.setattr(ses_notifications, "verify_sns_message", verified)
    monkeypatch.setattr(ses_notifications, "_confirm_subscription", forbidden)
    acquired = 0
    try:
        for _ in range(ses_notifications._SNS_CONFIRM_MAX_CONCURRENT):
            assert ses_notifications._SNS_CONFIRM_SLOTS.acquire(blocking=False)
            acquired += 1
        response = client.post(
            "/v1/internal/ses/notifications",
            json={
                "Type": "SubscriptionConfirmation",
                "MessageId": "capacity-confirmation",
                "TopicArn": "arn:aws:sns:us-east-1:123:ses-feedback",
                "SubscribeURL": "https://sns.us-east-1.amazonaws.com/confirm",
            },
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"
        assert outbound_calls == 0
    finally:
        for _ in range(acquired):
            ses_notifications._SNS_CONFIRM_SLOTS.release()
