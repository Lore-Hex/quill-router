"""User verification disclosure shared by delegated identity surfaces."""

from __future__ import annotations

from typing import Any

from trusted_router.storage import User


def verification_level(user: User | None) -> str:
    """Return the highest verified rung: identity > phone > email > none."""
    if user is None:
        return "none"
    if user.identity_verified:
        return "identity"
    if user.phone_verified:
        return "phone"
    if user.email_verified:
        return "email"
    return "none"


def identity_payload(user: User | None, workspace_id: str) -> dict[str, Any] | None:
    """Build the single identity shape used by exchange and userinfo."""
    if user is None:
        return None
    return {
        "sub": user.id,
        "email": user.email,
        "email_verified": user.email_verified,
        "phone_verified": user.phone_verified,
        "identity_verified": user.identity_verified,
        "verification_level": verification_level(user),
        "wallet_address": user.wallet_address,
        "workspace_id": workspace_id,
        "created_at": user.created_at,
    }
