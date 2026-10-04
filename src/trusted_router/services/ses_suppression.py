"""Mirror permanent SES feedback into the account-wide suppression list."""

from __future__ import annotations

from typing import Any, Literal

from trusted_router.config import Settings

SuppressionReason = Literal["BOUNCE", "COMPLAINT"]


class SesSuppressionSyncError(RuntimeError):
    """Raised when an SES account suppression write cannot be completed."""


class SesSuppressionService:
    """Lazily writes durable account-wide SES suppression entries."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    def suppress(self, email: str, reason: SuppressionReason) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.put_suppressed_destination(EmailAddress=email, Reason=reason)
        except Exception:
            # Do not include the recipient or the provider exception. SNS will
            # retry the privacy-safe webhook after the route returns a 503.
            raise SesSuppressionSyncError("SES account suppression write failed") from None

    def is_suppressed(self, email: str) -> bool:
        """Check account-wide blocks, including entries not received through SNS.

        An unavailable suppression lookup must not make an address eligible.
        The caller can retry discovery without claiming a customer notice.
        """
        client = self._get_client()
        if client is None:
            return False
        try:
            client.get_suppressed_destination(EmailAddress=email)
        except client.exceptions.NotFoundException:
            return False
        except Exception:
            raise SesSuppressionSyncError("SES account suppression read failed") from None
        return True

    def _get_client(self) -> Any | None:
        if not self._settings.aws_access_key_id or not self._settings.aws_secret_access_key:
            return None
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "sesv2",
                region_name=self._settings.aws_region,
                aws_access_key_id=self._settings.aws_access_key_id,
                aws_secret_access_key=self._settings.aws_secret_access_key,
            )
        return self._client
