"""Tests for the SES → SNS bounce/complaint webhook.

Signature verification is mocked because real X.509 + RSA round-trips
aren't worth running in unit tests, but the verification is exercised
end-to-end with a deliberately malformed cert URL to confirm we reject.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
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
from trusted_router.services.ses_suppression import (
    SesSuppressionService,
    SesSuppressionSyncError,
)
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


def _bounce_message(
    emails: list[str],
    bounce_type: str = "Permanent",
    *,
    tags: dict[str, list[str]] | None = None,
) -> str:
    return json.dumps({
        "notificationType": "Bounce",
        "bounce": {
            "bounceType": bounce_type,
            "feedbackId": "feedback-abc",
            "bouncedRecipients": [{"emailAddress": e} for e in emails],
        },
        "mail": {"messageId": "ses-msg-1", "tags": tags or {}},
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
    with (
        patch("trusted_router.routes.ses_notifications.verify_sns_message"),
        patch("trusted_router.routes.ses_notifications.SesSuppressionService.suppress") as suppress,
    ):
        resp = verified_client.post(
            "/internal/ses/notifications",
            json=envelope,
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["kind"] == "Bounce"
    assert resp.json()["data"]["blocked_count"] == 1
    assert STORE.is_email_blocked("BOUNCE@example.com")
    block = STORE.get_email_block("bounce@example.com")
    assert block is not None
    assert block.reason == "bounce"
    assert block.bounce_type == "Permanent"
    suppress.assert_called_once_with("bounce@example.com", "BOUNCE")


def test_transient_bounce_does_not_block(verified_client: TestClient) -> None:
    envelope = _envelope(
        MessageId="msg-bounce-2",
        Message=_bounce_message(["soft@example.com"], bounce_type="Transient"),
    )
    with (
        patch("trusted_router.routes.ses_notifications.verify_sns_message"),
        patch("trusted_router.routes.ses_notifications.SesSuppressionService.suppress") as suppress,
    ):
        verified_client.post("/internal/ses/notifications", json=envelope)
    assert not STORE.is_email_blocked("soft@example.com")
    suppress.assert_not_called()


def test_complaint_blocks_email(verified_client: TestClient) -> None:
    envelope = _envelope(MessageId="msg-complaint-1", Message=_complaint_message(["mad@example.com"]))
    with (
        patch("trusted_router.routes.ses_notifications.verify_sns_message"),
        patch("trusted_router.routes.ses_notifications.SesSuppressionService.suppress") as suppress,
    ):
        resp = verified_client.post("/internal/ses/notifications", json=envelope)
    assert resp.status_code == 200
    assert resp.json()["data"]["blocked_count"] == 1
    block = STORE.get_email_block("mad@example.com")
    assert block is not None and block.reason == "complaint"
    suppress.assert_called_once_with("mad@example.com", "COMPLAINT")


def test_suppression_sync_failure_keeps_sns_notification_retryable(
    verified_client: TestClient,
) -> None:
    envelope = _envelope(
        MessageId="msg-sync-failure",
        Message=_bounce_message(["retry@example.com"]),
    )
    with (
        patch("trusted_router.routes.ses_notifications.verify_sns_message"),
        patch(
            "trusted_router.routes.ses_notifications.SesSuppressionService.suppress",
            side_effect=SesSuppressionSyncError("sanitized"),
        ),
    ):
        response = verified_client.post("/internal/ses/notifications", json=envelope)

    assert response.status_code == 503
    assert STORE.is_email_blocked("retry@example.com")
    assert STORE.record_sns_message_once("msg-sync-failure") is True
    assert "retry@example.com" not in response.text


def test_account_suppression_service_writes_with_configured_region(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class FakeSesV2Client:
        def put_suppressed_destination(self, **kwargs: str) -> None:
            calls.append(kwargs)

    def fake_client(service: str, **kwargs: str) -> FakeSesV2Client:
        assert service == "sesv2"
        assert kwargs == {
            "region_name": "us-west-2",
            "aws_access_key_id": "AKIA_TEST",
            "aws_secret_access_key": "secret",
        }
        return FakeSesV2Client()

    monkeypatch.setattr("boto3.client", fake_client)
    service = SesSuppressionService(
        Settings(
            environment="test",
            aws_access_key_id="AKIA_TEST",
            aws_secret_access_key="secret",  # noqa: S106 - test fixture secret.
            aws_region="us-west-2",
        )
    )

    service.suppress("blocked@example.com", "BOUNCE")

    assert calls == [{"EmailAddress": "blocked@example.com", "Reason": "BOUNCE"}]


def test_account_suppression_service_sanitizes_provider_errors(monkeypatch) -> None:
    class FakeSesV2Client:
        def put_suppressed_destination(self, **_kwargs: str) -> None:
            raise RuntimeError("provider leaked private@example.com")

    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: FakeSesV2Client())
    service = SesSuppressionService(
        Settings(
            environment="test",
            aws_access_key_id="AKIA_TEST",
            aws_secret_access_key="secret",  # noqa: S106 - test fixture secret.
        )
    )

    with pytest.raises(SesSuppressionSyncError) as exc_info:
        service.suppress("private@example.com", "COMPLAINT")

    assert str(exc_info.value) == "SES account suppression write failed"
    assert exc_info.value.__cause__ is None


def test_replayed_message_id_is_idempotent(
    verified_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    envelope = _envelope(MessageId="dup-msg", Message=_bounce_message(["dup@example.com"]))
    with (
        patch("trusted_router.routes.ses_notifications.verify_sns_message"),
        caplog.at_level(logging.WARNING, logger="trusted_router.routes.ses_notifications"),
    ):
        first = verified_client.post("/internal/ses/notifications", json=envelope)
        second = verified_client.post("/internal/ses/notifications", json=envelope)
    assert first.json()["data"] == {
        "kind": "Bounce",
        "blocked_count": 1,
        "replayed": False,
    }
    assert second.json()["data"] == {
        "kind": "Bounce",
        "blocked_count": 0,
        "replayed": True,
    }
    assert sum(
        record.getMessage().startswith("ses_feedback.received ") for record in caplog.records
    ) == 1


def test_feedback_persists_privacy_safe_send_classification(
    verified_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    envelope = _envelope(
        MessageId="msg-attributed-bounce",
        Message=_bounce_message(
            ["campaign-recipient@example.com"],
            tags={
                "mail_class": ["email_verification"],
                "sender_profile": ["default"],
                "acquisition_source": ["google"],
                "acquisition_medium": ["paid_search"],
                "acquisition_campaign": ["legal-startups"],
            },
        ),
    )
    with (
        patch("trusted_router.routes.ses_notifications.verify_sns_message"),
        caplog.at_level(logging.WARNING, logger="trusted_router.routes.ses_notifications"),
    ):
        response = verified_client.post("/internal/ses/notifications", json=envelope)

    assert response.status_code == 200
    assert "campaign-recipient@example.com" not in response.text
    block = STORE.get_email_block("campaign-recipient@example.com")
    assert block is not None
    assert block.mail_class == "email_verification"
    assert block.sender_profile == "default"
    assert block.acquisition_source == "google"
    assert block.acquisition_medium == "paid_search"
    assert block.acquisition_campaign == "legal-startups"
    feedback_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("ses_feedback.received ")
    ]
    assert len(feedback_records) == 1
    feedback_log = feedback_records[0].getMessage()
    assert "campaign-recipient@example.com" not in feedback_log
    assert "class=email_verification" in feedback_log
    assert "source=google" in feedback_log
    assert feedback_records[0].ses_message_id == "ses-msg-1"
    assert feedback_records[0].feedback_id == "feedback-abc"


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


def test_email_service_fallback_logs_body_length_not_body(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(environment="local")
    service = EmailService(settings)
    body = "Verify with token=secret-token and free-text sensitive content."
    subject_marker = "SubjectUserMarker-4837"
    subject = f"Confirm account for {subject_marker}"

    with caplog.at_level(logging.INFO, logger="trusted_router.services.email"):
        sent = service.send(
            EmailMessage(
                to="user@example.com",
                subject=subject,
                text_body=body,
            )
        )

    assert sent is False
    fallback_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "trusted_router.services.email"
        and record.getMessage().startswith("email_send.fallback ")
    ]
    fingerprint = hashlib.sha256(b"user@example.com").hexdigest()[:16]
    assert fallback_logs == [
        f"email_send.fallback recipient={fingerprint} class=transactional "
        f"subject_len={len(subject)} body_len={len(body)}"
    ]
    assert "user@example.com" not in fallback_logs[0]
    assert "subject=" not in fallback_logs[0]
    assert subject_marker not in fallback_logs[0]
    assert subject not in fallback_logs[0]
    assert "body=" not in fallback_logs[0]
    assert body not in fallback_logs[0]


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
    assert {tag["Name"]: tag["Value"] for tag in call["Tags"]} == {
        "mail_class": "email_verification",
        "sender_profile": "default",
    }


def test_email_service_uses_dedicated_alert_sender_and_sanitized_tags(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeSESClient:
        def send_email(self, **kwargs: Any) -> dict[str, str]:
            calls.append(kwargs)
            return {"MessageId": "ses-alert-1"}

    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: FakeSESClient())
    service = EmailService(
        Settings(
            environment="test",
            aws_access_key_id="AKIA_TEST",
            aws_secret_access_key="secret",  # noqa: S106 - test fixture secret.
            ses_from_email="noreply@example.com",
            ses_alert_from_email="alerts@alerts.example.com",
            ses_alert_from_name="Example Alerts",
            ses_alert_configuration_set="example-alerts",
        )
    )

    with caplog.at_level(logging.INFO, logger="trusted_router.services.email"):
        assert service.send(
            EmailMessage(
                to="owner@example.com",
                subject="Budget alert",
                text_body="Budget crossed.",
                mail_class="budget alert",
                sender_profile="alerts",
                acquisition_source="ads.example/path",
                acquisition_campaign="Legal teams: Q3",
            )
        )

    call = calls[0]
    assert call["Source"] == "Example Alerts <alerts@alerts.example.com>"
    assert call["ConfigurationSetName"] == "example-alerts"
    assert {tag["Name"]: tag["Value"] for tag in call["Tags"]} == {
        "mail_class": "budget-alert",
        "sender_profile": "alerts",
        "acquisition_source": "ads-example-path",
        "acquisition_campaign": "Legal-teams-Q3",
    }
    accepted = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "email_send.accepted"
    ]
    assert len(accepted) == 1
    assert accepted[0].ses_message_id == "ses-alert-1"


def test_alert_sender_never_falls_back_to_default_identity(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeSESClient:
        def send_email(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: FakeSESClient())
    service = EmailService(
        Settings(
            environment="test",
            aws_access_key_id="AKIA_TEST",
            aws_secret_access_key="secret",  # noqa: S106 - test fixture secret.
            ses_from_email="noreply@example.com",
            ses_alert_from_email=None,
        )
    )

    sent = service.send(
        EmailMessage(
            to="owner@example.com",
            subject="Budget alert",
            text_body="Budget crossed.",
            mail_class="budget_alert",
            sender_profile="alerts",
        )
    )

    assert sent is False
    assert calls == []


def test_email_service_sets_reply_to_for_support_mail(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeSESClient:
        def send_email(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: FakeSESClient())
    service = EmailService(
        Settings(
            environment="test",
            aws_access_key_id="AKIA_TEST",
            aws_secret_access_key="secret",  # noqa: S106 - test fixture secret.
            ses_from_email="noreply@example.com",
        )
    )

    assert service.send(
        EmailMessage(
            to="help@example.com",
            reply_to="customer@example.com",
            subject="Support request",
            text_body="Please help.",
        )
    )
    assert calls[0]["ReplyToAddresses"] == ["customer@example.com"]


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


def _self_signed_cert_and_key() -> tuple[Any, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.us-east-1.amazonaws.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert, key


def test_sns_verify_matches_aws_spec_canonical_string_v1() -> None:
    """Sign a HAND-BUILT canonical string (per the AWS docs) rather than the
    implementation's own _canonical_string, so a spec deviation in the
    implementation cannot hide behind a circular round-trip."""
    cert, key = _self_signed_cert_and_key()
    message = _envelope(SignatureVersion="1", Signature="")
    canonical = (
        f"Message\n{message['Message']}\n"
        f"MessageId\n{message['MessageId']}\n"
        f"Timestamp\n{message['Timestamp']}\n"
        f"TopicArn\n{message['TopicArn']}\n"
        f"Type\n{message['Type']}\n"
    )
    signature = key.sign(canonical.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())  # noqa: S303 - SigV1 is SHA1 by AWS spec
    message["Signature"] = base64.b64encode(signature).decode("ascii")
    verify_sns_message(
        message, cert_fetcher=lambda url: cert.public_bytes(serialization.Encoding.PEM)
    )


def test_sns_verify_excludes_null_subject_like_aws_does() -> None:
    """SES notifications arrive with "Subject": null; AWS signs WITHOUT the
    Subject lines in that case and verification must agree."""
    cert, key = _self_signed_cert_and_key()
    message = _envelope(SignatureVersion="1", Signature="", Subject=None)
    canonical = (
        f"Message\n{message['Message']}\n"
        f"MessageId\n{message['MessageId']}\n"
        f"Timestamp\n{message['Timestamp']}\n"
        f"TopicArn\n{message['TopicArn']}\n"
        f"Type\n{message['Type']}\n"
    )
    signature = key.sign(canonical.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())  # noqa: S303 - SigV1 is SHA1 by AWS spec
    message["Signature"] = base64.b64encode(signature).decode("ascii")
    verify_sns_message(
        message, cert_fetcher=lambda url: cert.public_bytes(serialization.Encoding.PEM)
    )


def test_sns_verify_rejects_raw_message_delivery_envelope() -> None:
    """A subscription with RawMessageDelivery=true delivers the bare SES
    feedback JSON — no Type, no Signature. That must be rejected (it is
    unverifiable), and the reason should point operators at the cause."""
    raw_feedback = {
        "notificationType": "Bounce",
        "bounce": {"bounceType": "Permanent", "bouncedRecipients": [{"emailAddress": "x@y.com"}]},
    }
    with pytest.raises(SnsVerificationError, match="unsupported SNS Type"):
        verify_sns_message(raw_feedback, cert_fetcher=lambda url: b"")
