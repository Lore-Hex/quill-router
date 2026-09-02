from __future__ import annotations

import re

from trusted_router.storage_models import User

CREATOR_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
CREATOR_USERNAME_RESERVED = frozenset(
    {
        "admin",
        "api",
        "billing",
        "console",
        "docs",
        "lorehex",
        "official",
        "security",
        "support",
        "tr",
        "trustedrouter",
        "trusted-router",
    }
)


def normalize_creator_username(value: str) -> str:
    return value.strip().lower()


def validate_creator_username(value: str) -> str:
    username = normalize_creator_username(value)
    if not CREATOR_USERNAME_PATTERN.fullmatch(username):
        raise ValueError("invalid_creator_username")
    if username in CREATOR_USERNAME_RESERVED:
        raise ValueError("creator_username_reserved")
    return username


def local_creator_username(user: User) -> str:
    """Stable local/test namespace when production verification is disabled."""
    if user.username:
        return validate_creator_username(user.username)
    local_part = (user.email or "").partition("@")[0].lower()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", local_part).strip("-")[:32]
    if (
        not CREATOR_USERNAME_PATTERN.fullmatch(cleaned)
        or cleaned in CREATOR_USERNAME_RESERVED
    ):
        compact_id = re.sub(r"[^a-z0-9]+", "", user.id.lower())[:12]
        cleaned = f"dev-{compact_id or 'creator'}"
    return cleaned


def creator_username_for_models(user: User, *, enforce_verification: bool) -> str:
    if user.username:
        return validate_creator_username(user.username)
    if enforce_verification:
        raise ValueError("creator_username_required")
    return local_creator_username(user)
