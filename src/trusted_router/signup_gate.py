from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

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


DISPOSABLE_EMAIL_MESSAGE = (
    "Accounts cannot be created with a disposable email address. "
    "Sign in with a Google or GitHub account whose primary email is permanent."
)

_DISPOSABLE_EMAIL_DOMAINS_PATH = Path(__file__).parent / "data" / "disposable_email_domains.txt"

# Domains seen in abuse that the vendored upstream list does not carry yet.
# Upstream them when convenient; keeping them here survives a list refresh.
LOCAL_DISPOSABLE_EMAIL_DOMAINS = frozenset({
    # 2026-11 stolen-PayPal top-up ring.
    "qtymail.com",
})


@cache
def disposable_email_domains() -> frozenset[str]:
    """The vendored upstream list plus the local additions, lowercased."""
    lines = _DISPOSABLE_EMAIL_DOMAINS_PATH.read_text(encoding="utf-8").splitlines()
    vendored = {
        line.strip().lower() for line in lines if line.strip() and not line.startswith("#")
    }
    return frozenset(vendored | LOCAL_DISPOSABLE_EMAIL_DOMAINS)


def is_disposable_email(email: str | None) -> bool:
    """Whether the address is at a disposable domain or a subdomain of one."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    labels = domain.split(".")
    blocked = disposable_email_domains()
    # foo.example.com checks foo.example.com and example.com, never bare "com".
    return any(".".join(labels[i:]) in blocked for i in range(len(labels) - 1))


def require_non_disposable_email(email: str | None) -> None:
    """Reject a code path that is about to create an account for this address.

    Returning users are untouched: callers invoke this only on the branch that
    creates a user, next to ``require_new_account_creation``.
    """
    if is_disposable_email(email):
        raise api_error(403, DISPOSABLE_EMAIL_MESSAGE, ErrorType.FORBIDDEN)


WALLET_SIGNUPS_DISABLED_MESSAGE = (
    "New accounts cannot be created with a wallet. Sign in with Google or GitHub. "
    "Wallets that already have an account can still sign in."
)


def wallet_account_creation_allowed(settings: Settings) -> bool:
    return settings.new_signups_enabled and settings.wallet_signup_enabled


def require_wallet_account_creation(settings: Settings) -> None:
    """Reject a code path that is about to create an account for a wallet."""
    require_new_account_creation(settings)
    if not settings.wallet_signup_enabled:
        raise api_error(
            403,
            WALLET_SIGNUPS_DISABLED_MESSAGE,
            ErrorType.FORBIDDEN,
        )


def starter_credit_for_email(email: str, starter_credit_microdollars: int) -> int:
    """Starter credit a new account with this address receives.

    Hotmail and Outlook addresses (hotmail.com, outlook.com, hotmail.co.uk, ...)
    receive none: they are free to mint in bulk and carried the 2026-10-14
    starter-credit farming waves.
    """
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if domain.startswith(("hotmail.", "outlook.")):
        return 0
    return starter_credit_microdollars


# Google sign-ups on these consumer domains always receive starter credit.
GOOGLE_UNCAPPED_STARTER_CREDIT_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

# Google sign-ups on any other domain (a Google Workspace or a non-Google
# address registered as a Google account) receive starter credit only for the
# first this-many accounts on that domain. One Workspace domain mints unlimited
# verified addresses: the 2026-11-12 wave created 50 on one domain in an hour.
STARTER_CREDIT_ACCOUNTS_PER_DOMAIN = 3


def google_starter_credit_domain_limit(email: str) -> int | None:
    """Per-domain starter-credit limit for a Google sign-up, None for uncapped."""
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if domain in GOOGLE_UNCAPPED_STARTER_CREDIT_DOMAINS:
        return None
    return STARTER_CREDIT_ACCOUNTS_PER_DOMAIN


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
