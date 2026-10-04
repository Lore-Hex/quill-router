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
