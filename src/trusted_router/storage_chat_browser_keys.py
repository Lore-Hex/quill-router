"""Shared invariants for automatically issued chat-browser API keys.

The route returns the raw key exactly once.  Storage persists only the same
salted hash, lookup hash, and short label as every other API key; this module
never accepts a previously stored secret and therefore cannot recover one.
"""

from __future__ import annotations

import datetime as dt

from trusted_router.security import (
    hash_api_key,
    key_label,
    lookup_hash_api_key,
    new_api_key,
    new_hash_salt,
    new_key_id,
)
from trusted_router.storage_models import ApiKey

CHAT_BROWSER_KEY_NAME_PREFIX = "chat-browser-"
CHAT_BROWSER_KEY_TAG = "trustedrouter_key_kind"
CHAT_BROWSER_KEY_TAG_VALUE = "chat-browser"
CHAT_BROWSER_KEY_GUARD_KIND = "chat_browser_key_guard"


def is_active_chat_browser_key(
    key: ApiKey,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Return whether ``key`` consumes one durable browser-key slot.

    The name fallback includes keys issued before the purpose tag existed.
    An invalid expiry is treated as expired, matching API-key authentication's
    fail-closed timestamp handling.
    """
    is_browser_key = (
        key.tags.get(CHAT_BROWSER_KEY_TAG) == CHAT_BROWSER_KEY_TAG_VALUE
        or key.name.startswith(CHAT_BROWSER_KEY_NAME_PREFIX)
    )
    if not is_browser_key or key.disabled:
        return False
    if key.expires_at is None:
        return True
    try:
        expires_at = dt.datetime.fromisoformat(key.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    instant = now or dt.datetime.now(dt.UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.UTC)
    return expires_at > instant


def new_chat_browser_api_key(
    *,
    workspace_id: str,
    name: str,
    creator_user_id: str,
    limit_microdollars: int,
    expires_at: str,
) -> tuple[str, ApiKey]:
    """Create the one-shot raw value and its hash-only durable record."""
    raw = new_api_key()
    salt = new_hash_salt()
    key = ApiKey(
        hash=new_key_id(),
        salt=salt,
        secret_hash=hash_api_key(raw, salt),
        lookup_hash=lookup_hash_api_key(raw),
        name=name,
        label=key_label(raw),
        workspace_id=workspace_id,
        creator_user_id=creator_user_id,
        management=False,
        limit_microdollars=limit_microdollars,
        include_byok_in_limit=True,
        expires_at=expires_at,
        tags={CHAT_BROWSER_KEY_TAG: CHAT_BROWSER_KEY_TAG_VALUE},
        usage_shard_count=1,
    )
    return raw, key


def validate_chat_browser_key_cap(active_key_cap: int) -> None:
    if active_key_cap < 1:
        raise ValueError("active chat-browser key cap must be positive")
