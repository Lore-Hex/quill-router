"""Transactional email via Amazon SES.

Every send is classified before it reaches SES. The classification is used
for reputation telemetry and bounce attribution, but never includes the raw
recipient address or message content.

Local-dev fallback: when SES credentials are absent the service logs the
verification URL to stdout instead of attempting an SMTP send. Routes
that depend on SES use `settings.ses_enabled` to decide whether to fall
back to the dev-only "copy this link" UX.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Literal

from trusted_router.config import Settings
from trusted_router.storage import STORE

log = logging.getLogger(__name__)

SenderProfile = Literal["default", "alerts"]
_SES_TAG_VALUE_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text_body: str
    html_body: str | None = None
    reply_to: str | None = None
    mail_class: str = "transactional"
    sender_profile: SenderProfile = "default"
    acquisition_source: str | None = None
    acquisition_medium: str | None = None
    acquisition_campaign: str | None = None


class EmailService:
    """Thin wrapper around boto3 SES. Constructed with the live settings;
    falls back to a stdout logger when SES isn't configured."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None
        if settings.ses_enabled:
            try:
                import boto3

                self._client = boto3.client(
                    "ses",
                    region_name=settings.aws_region,
                    aws_access_key_id=settings.aws_access_key_id,
                    aws_secret_access_key=settings.aws_secret_access_key,
                )
            except ImportError:  # pragma: no cover - boto3 is in dependencies.
                log.warning("boto3 not installed; email sending disabled")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def send(self, message: EmailMessage) -> bool:
        """Returns True iff the message was handed off to SES. False means
        the caller should display the URL inline (dev), refuse the action
        (prod with no SES configured), or skip silently (the address is on
        our suppression list from a prior bounce/complaint)."""
        if STORE.is_email_blocked(message.to):
            block = STORE.get_email_block(message.to)
            log.warning(
                "email_send.suppressed recipient=%s reason=%s class=%s",
                _recipient_fingerprint(message.to),
                block.reason if block else "unknown",
                message.mail_class,
            )
            return False
        if self._client is None:
            log.info(
                "email_send.fallback recipient=%s class=%s subject_len=%d body_len=%d",
                _recipient_fingerprint(message.to),
                message.mail_class,
                len(message.subject),
                len(message.text_body),
            )
            return False
        body: dict[str, dict[str, str]] = {"Text": {"Data": message.text_body, "Charset": "UTF-8"}}
        if message.html_body:
            body["Html"] = {"Data": message.html_body, "Charset": "UTF-8"}
        sender = _sender_for_message(self._settings, message)
        if sender is None:
            log.error(
                "email_send.sender_unconfigured recipient=%s class=%s profile=%s",
                _recipient_fingerprint(message.to),
                message.mail_class,
                message.sender_profile,
            )
            return False
        from_name, from_email, configuration_set = sender
        from_address = f"{from_name} <{from_email}>"
        kwargs: dict[str, object] = {
            "Source": from_address,
            "Destination": {"ToAddresses": [message.to]},
            "Message": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": body,
            },
            "Tags": _message_tags(message),
        }
        if message.reply_to:
            kwargs["ReplyToAddresses"] = [message.reply_to]
        # The configuration set wires bounce + complaint events to our SNS
        # topic so the suppression list above stays current. Skip on missing
        # config set so local dev with a half-set-up SES doesn't 400.
        if configuration_set:
            kwargs["ConfigurationSetName"] = configuration_set
        response = self._client.send_email(**kwargs)
        message_id = response.get("MessageId") if isinstance(response, dict) else None
        log.info(
            "email_send.accepted recipient=%s class=%s profile=%s message_id=%s",
            _recipient_fingerprint(message.to),
            message.mail_class,
            message.sender_profile,
            message_id or "unknown",
            extra={
                "event": "email_send.accepted",
                "ses_message_id": str(message_id or "unknown"),
                "mail_class": message.mail_class,
                "sender_profile": message.sender_profile,
                "acquisition_source": message.acquisition_source or "unknown",
                "acquisition_medium": message.acquisition_medium or "unknown",
                "acquisition_campaign": message.acquisition_campaign or "unknown",
            },
        )
        return True


def _sender_for_message(
    settings: Settings,
    message: EmailMessage,
) -> tuple[str, str, str | None] | None:
    if message.sender_profile == "alerts":
        if not settings.ses_alert_from_email or not settings.ses_alert_configuration_set:
            return None
        return (
            settings.ses_alert_from_name,
            settings.ses_alert_from_email,
            settings.ses_alert_configuration_set,
        )
    if message.sender_profile != "default" or not settings.ses_from_email:
        return None
    return settings.ses_from_name, settings.ses_from_email, settings.ses_configuration_set


def _recipient_fingerprint(email: str) -> str:
    normalized = email.strip().lower().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def _ses_tag_value(value: str) -> str:
    normalized = _SES_TAG_VALUE_RE.sub("-", value.strip()).strip("-_")
    return (normalized or "unknown")[:256]


def _message_tags(message: EmailMessage) -> list[dict[str, str]]:
    values = {
        "mail_class": message.mail_class,
        "sender_profile": message.sender_profile,
        "acquisition_source": message.acquisition_source,
        "acquisition_medium": message.acquisition_medium,
        "acquisition_campaign": message.acquisition_campaign,
    }
    return [
        {"Name": name, "Value": _ses_tag_value(value)}
        for name, value in values.items()
        if value
    ]


def build_verification_email(
    *,
    to: str,
    verification_url: str,
    from_name: str = "TrustedRouter",
    acquisition_source: str | None = None,
    acquisition_medium: str | None = None,
    acquisition_campaign: str | None = None,
) -> EmailMessage:
    subject = f"Confirm your {from_name} account"
    text = (
        f"Welcome to {from_name}.\n\n"
        f"Click this link to confirm your email address and finish creating your account:\n\n"
        f"{verification_url}\n\n"
        "The link expires in 24 hours. If you didn't try to create an account, you can ignore this email."
    )
    html = (
        f"<p>Welcome to {from_name}.</p>"
        f"<p>Click the link below to confirm your email address and finish creating your account:</p>"
        f'<p><a href="{verification_url}">Confirm my email</a></p>'
        "<p>The link expires in 24 hours. If you didn't try to create an account, you can ignore this email.</p>"
    )
    return EmailMessage(
        to=to,
        subject=subject,
        text_body=text,
        html_body=html,
        mail_class="email_verification",
        acquisition_source=acquisition_source,
        acquisition_medium=acquisition_medium,
        acquisition_campaign=acquisition_campaign,
    )


def get_email_service(settings: Settings) -> EmailService:
    return EmailService(settings)
