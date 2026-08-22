from __future__ import annotations

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.types import ErrorType

NEW_SIGNUPS_DISABLED_MESSAGE = (
    "New account creation is temporarily disabled. Existing users can still sign in."
)


def require_new_account_creation(settings: Settings) -> None:
    """Reject only a code path that is about to create a user account."""
    if not settings.new_signups_enabled:
        raise api_error(
            403,
            NEW_SIGNUPS_DISABLED_MESSAGE,
            ErrorType.FORBIDDEN,
        )
