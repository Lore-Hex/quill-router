"""Account-level prerequisites for phone and identity verification."""

from __future__ import annotations

from trusted_router.config import Settings
from trusted_router.money import VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS
from trusted_router.storage import STORE
from trusted_router.storage_models import User


def missing_phone_verification_requirements(user: User, settings: Settings) -> list[str]:
    missing: list[str] = []
    if not user.email:
        missing.append("email")
    if (
        settings.phone_verification_funding_enforced
        and STORE.get_lifetime_topup_microdollars(user.id) <= 0
    ):
        missing.append("funding")
    return missing


def missing_identity_verification_requirements(
    user: User,
    settings: Settings,
    *,
    lifetime_topup_microdollars: int | None = None,
) -> list[str]:
    missing: list[str] = []
    if not user.email:
        missing.append("email")
    if not user.phone_verified:
        missing.append("phone_verified")
    lifetime_topup = (
        STORE.get_lifetime_topup_microdollars(user.id)
        if lifetime_topup_microdollars is None
        else lifetime_topup_microdollars
    )
    if lifetime_topup < VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS:
        missing.append("funding")
    return missing
