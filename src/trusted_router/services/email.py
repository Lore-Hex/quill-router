"""Transactional email via Amazon SES.

Used today only for the wallet-user email-verification flow (Google and
GitHub already provide a verified email through OIDC so they skip SES).
The service is intentionally narrow — one method, two templates — so it
stays easy to mock in tests and swap for a different provider later.

Local-dev fallback: when SES credentials are absent the service logs the
verification URL to stdout instead of attempting an SMTP send. Routes
that depend on SES use `settings.ses_enabled` to decide whether to fall
back to the dev-only "copy this link" UX.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from trusted_router.config import Settings
from trusted_router.storage import STORE

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text_body: str
    html_body: str | None = None


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
                "email_send.suppressed to=%s reason=%s",
                message.to,
                block.reason if block else "unknown",
            )
            return False
        if self._client is None:
            log.info("email_send.fallback to=%s subject=%s body=%s", message.to, message.subject, message.text_body)
            return False
        body: dict[str, dict[str, str]] = {"Text": {"Data": message.text_body, "Charset": "UTF-8"}}
        if message.html_body:
            body["Html"] = {"Data": message.html_body, "Charset": "UTF-8"}
        from_address = (
            f"{self._settings.ses_from_name} <{self._settings.ses_from_email}>"
            if self._settings.ses_from_email
            else "noreply@trustedrouter.com"
        )
        kwargs: dict[str, object] = {
            "Source": from_address,
            "Destination": {"ToAddresses": [message.to]},
            "Message": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": body,
            },
        }
        # The configuration set wires bounce + complaint events to our SNS
        # topic so the suppression list above stays current. Skip on missing
        # config set so local dev with a half-set-up SES doesn't 400.
        if self._settings.ses_configuration_set:
            kwargs["ConfigurationSetName"] = self._settings.ses_configuration_set
        self._client.send_email(**kwargs)
        return True


def build_verification_email(
    *,
    to: str,
    verification_url: str,
    from_name: str = "TrustedRouter",
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
    return EmailMessage(to=to, subject=subject, text_body=text, html_body=html)


def get_email_service(settings: Settings) -> EmailService:
    return EmailService(settings)
