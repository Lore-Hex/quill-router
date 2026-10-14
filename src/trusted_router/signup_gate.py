from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def starter_credit_for_email(email: str, starter_credit_microdollars: int) -> int:
    """Starter credit a new account with this address receives.

    Hotmail addresses (hotmail.com, hotmail.co.uk, ...) receive none: they are
    free to mint in bulk and carried the 2026-10-14 starter-credit farming wave.
    """
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if domain.startswith("hotmail."):
        return 0
    return starter_credit_microdollars


# A GitHub account younger than this receives no starter credit.
GITHUB_STARTER_CREDIT_MIN_ACCOUNT_AGE = timedelta(days=30)


def github_account_old_enough(created_at: str | None, *, now: datetime | None = None) -> bool:
    """Whether a GitHub account created at `created_at` may receive starter credit.

    A missing, unparseable or timezone-less creation time is not old enough.
    """
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        return False
    return (now or datetime.now(UTC)) - created >= GITHUB_STARTER_CREDIT_MIN_ACCOUNT_AGE
