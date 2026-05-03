"""Tests for the SES → SNS bounce/complaint webhook.

Signature verification is mocked because real X.509 + RSA round-trips
aren't worth running in unit tests, but the verification is exercised
end-to-end with a deliberately malformed cert URL to confirm we reject.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.services.email import EmailMessage, EmailService, build_verification_email
from trusted_router.sns_verify import SnsVerificationError, verify_sns_message
from trusted_router.storage import STORE


def _envelope(**overrides: Any) -> dict[str, Any]:
    base = {
        "Type": "Notification",
        "MessageId": "msg-1",
        "TopicArn": "arn:aws:sns:us-east-1:123:ses-feedback",
        "Message": json.dumps({"notificationType": "Bounce"}),
        "Timestamp": "2026-05-02T00:00:00Z",
        "SignatureVersion": "1",
        "Signature": "fake",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-x.pem",
    }
    base.update(overrides)
    return base


def _bounce_message(emails: list[str], bounce_type: str = "Permanent") -> str:
    return json.dumps({
        "notificationType": "Bounce",
        "bounce": {
            "bounceType": bounce_type,
            "feedbackId": "feedback-abc",
            "bouncedRecipients": [{"emailAddress": e} for e in emails],
        },
        "mail": {"messageId": "ses-msg-1"},
    })


def _complaint_message(emails: list[str]) -> str:
    return json.dumps({
        "notificationType": "Complaint",
        "complaint": {
            "feedbackId": "feedback-def",
            "complainedRecipients": [{"emailAddress": e} for e in emails],
        },
        "mail": {"messageId": "ses-msg-2"},
    })


@pytest.fixture
def verified_client() -> TestClient:
    """A TestClient where the SNS signature check is bypassed."""
    from trusted_router.main import app

    client = TestClient(app)
    return client


def test_permanent_bounce_blocks_email(verified_client: TestClient) -> None:
    envelope = _envelope(MessageId="msg-bounce-1", Message=_bounce_message(["bounce@example.com"]))
    with patch("trusted_router.routes.ses_notifications.verify_sns_message"):
        resp = verified_client.post(
            "/internal/ses/notifications",
            json=envelope,
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["kind"] == "Bounce"
    assert resp.json()["data"]["blocked_emails"] == ["bounce@example.com"]
    assert STORE.is_email_blocked("BOUNCE@example.com")
    block = STORE.get_email_block("bounce@example.com")
    assert block is not None
    assert block.reason == "bounce"
    assert block.bounce_type == "Permanent"


def test_transient_bounce_does_not_block(verified_client: TestClient) -> None:
    envelope = _envelope(
        MessageId="msg-bounce-2",
        Message=_bounce_message(["soft@example.com"], bounce_type="Transient"),
    )
    with patch("trusted_router.routes.ses_notifications.verify_sns_message"):
        verified_client.post("/internal/ses/notifications", json=envelope)
    assert not STORE.is_email_blocked("soft@example.com")


def test_complaint_blocks_email(verified_client: TestClient) -> None:
    envelope = _envelope(MessageId="msg-complaint-1", Message=_complaint_message(["mad@example.com"]))
    with patch("trusted_router.routes.ses_notifications.verify_sns_message"):
        resp = verified_client.post("/internal/ses/notifications", json=envelope)
    assert resp.status_code == 200
    assert resp.json()["data"]["blocked_emails"] == ["mad@example.com"]
    block = STORE.get_email_block("mad@example.com")
    assert block is not None and block.reason == "complaint"


def test_replayed_message_id_is_idempotent(verified_client: TestClient) -> None:
    envelope = _envelope(MessageId="dup-msg", Message=_bounce_message(["dup@example.com"]))
    with patch("trusted_router.routes.ses_notifications.verify_sns_message"):
        first = verified_client.post("/internal/ses/notifications", json=envelope)
        second = verified_client.post("/internal/ses/notifications", json=envelope)
    assert first.json()["data"].get("blocked_emails") == ["dup@example.com"]
    assert second.json()["data"] == {"replayed": True, "message_id": "dup-msg"}


def test_signature_failure_returns_403(verified_client: TestClient) -> None:
    envelope = _envelope(MessageId="msg-sig", Message=_bounce_message(["x@example.com"]))

    def fail(_envelope: dict[str, Any], **_kwargs: Any) -> None:
        raise SnsVerificationError("forged")

    with patch("trusted_router.routes.ses_notifications.verify_sns_message", side_effect=fail):
        resp = verified_client.post("/internal/ses/notifications", json=envelope)
    assert resp.status_code == 403
    # The forged email must NOT be blocked.
    assert not STORE.is_email_blocked("x@example.com")


def test_subscription_confirmation_calls_subscribe_url(verified_client: TestClient) -> None:
    envelope = _envelope(
        Type="SubscriptionConfirmation",
        MessageId="msg-sub-1",
        SubscribeURL="https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=tok",
        Message="Hello",
    )

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    with patch("trusted_router.routes.ses_notifications.verify_sns_message"), \
         patch("trusted_router.routes.ses_notifications.httpx.get", return_value=FakeResponse()) as fake_get:
        resp = verified_client.post("/internal/ses/notifications", json=envelope)
    assert resp.status_code == 200
    assert resp.json()["data"]["confirmed"] is True
    fake_get.assert_called_once()


def test_email_service_skips_blocked_recipient() -> None:
    settings = Settings(environment="local")
    service = EmailService(settings)
    STORE.block_email_sending(email="blocked@example.com", reason="bounce")
    sent = service.send(
        EmailMessage(
            to="blocked@example.com",
            subject="Welcome",
            text_body="Click to verify",
        )
    )
    assert sent is False


def test_email_service_sends_expected_ses_payload(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeSESClient:
        def send_email(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    def fake_client(service: str, **kwargs: Any) -> FakeSESClient:
        assert service == "ses"
        assert kwargs["region_name"] == "us-west-2"
        assert kwargs["aws_access_key_id"] == "AKIA_TEST"
        assert kwargs["aws_secret_access_key"] == "secret"  # noqa: S105 - expected test secret.
        return FakeSESClient()

    monkeypatch.setattr("boto3.client", fake_client)
    service = EmailService(
        Settings(
            environment="test",
            aws_access_key_id="AKIA_TEST",
            aws_secret_access_key="secret",  # noqa: S106 - test fixture secret.
            aws_region="us-west-2",
            ses_from_email="noreply@example.com",
            ses_from_name="TrustedRouter Test",
        )
    )

    sent = service.send(
        build_verification_email(
            to="user@example.com",
            verification_url="https://trustedrouter.com/auth/verify-email?token=tok",
            from_name="TrustedRouter Test",
        )
    )

    assert sent is True
    assert len(calls) == 1
    call = calls[0]
    # Behaviour assertions: we want a from-address with the configured display
    # name, the recipient in the To line, the right subject, and the message
    # body to actually carry the verification link. Don't lock in the literal
    # boto3 kwarg shape — that would re-fail every time AWS adds a parameter.
    assert call["Source"] == "TrustedRouter Test <noreply@example.com>"
    assert call["Destination"]["ToAddresses"] == ["user@example.com"]
    assert call["Message"]["Subject"]["Data"] == "Confirm your TrustedRouter Test account"
    assert "https://trustedrouter.com/auth/verify-email?token=tok" in call["Message"]["Body"]["Text"]["Data"]
    assert "Confirm my email" in call["Message"]["Body"]["Html"]["Data"]
    # Routes the send through the configuration set so SES emits bounce +
    # complaint events to the SNS topic our /internal/ses/notifications owns.
    assert call.get("ConfigurationSetName") == "trustedrouter-default"


def test_sns_verify_rejects_non_amazonaws_cert_url() -> None:
    msg = _envelope(SigningCertURL="https://evil.example.com/cert.pem")
    with pytest.raises(SnsVerificationError):
        verify_sns_message(msg)


def test_sns_verify_rejects_unknown_signature_version() -> None:
    msg = _envelope(SignatureVersion="3")
    with pytest.raises(SnsVerificationError):
        verify_sns_message(msg)


def test_sns_verify_accepts_valid_sha256_rsa_signature() -> None:
    from trusted_router.sns_verify import _canonical_string

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "sns.us-east-1.amazonaws.com")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    message = _envelope(
        SignatureVersion="2",
        SigningCertURL="https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem",
        Signature="",
    )
    signature = key.sign(
        _canonical_string(
            message,
            ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
        ).encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    message["Signature"] = base64.b64encode(signature).decode("ascii")

    verify_sns_message(
        message,
        cert_fetcher=lambda url: cert.public_bytes(serialization.Encoding.PEM),
    )
