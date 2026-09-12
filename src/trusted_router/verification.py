"""User verification disclosure shared by delegated identity surfaces."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

import anyio

from trusted_router.company_affiliations import AffiliationDirectory
from trusted_router.storage import STORE, User

AFFILIATION_TIMEOUT_SECONDS = 1.0


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


async def enriched_identity_payload(user: User | None, workspace_id: str) -> dict[str, Any] | None:
    """Optional profile enrichment must never become a sign-in dependency."""
    identity = identity_payload(user, workspace_id)
    if identity is None or user is None or user.email_verified is not True:
        return identity
    directory: AffiliationDirectory = STORE.get_company_affiliation_directory()
    try:
        async with asyncio.timeout(AFFILIATION_TIMEOUT_SECONDS):
            affiliations = await anyio.to_thread.run_sync(
                partial(directory.lookup, user.email, email_verified=True),
                abandon_on_cancel=True,
            )
    except TimeoutError:
        directory.defer_retries()
        return identity
    if affiliations:
        identity["company_affiliations"] = affiliations
    return identity
