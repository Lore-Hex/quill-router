"""Shared claim-request admission, separate from the once-only credit receipt."""
from __future__ import annotations

import logging

from fastapi import HTTPException

from trusted_router.config import Settings
from trusted_router.request_limits import fingerprint_subject, rate_limit_headers
from trusted_router.storage import STORE

log = logging.getLogger(__name__)


def enforce_social_reward_rate_limit(user_id: str, settings: Settings) -> None:
    """A session refresh or another serving instance must not reset this quota.

    This limits requests, not promotional dollars, and does not identify people
    behind distinct accounts. Five immediate rewards plus retries fit the default.
    Storage failure must not silently remove admission from the grant endpoint.
    """
    try:
        hit = STORE.hit_rate_limit(
            namespace="social_rewards_claim_v1",
            subject=fingerprint_subject(user_id),
            limit=settings.social_rewards_claims_per_minute,
            window_seconds=60,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed without exposing storage details.
        log.warning("social_rewards.admission_unavailable", extra={"error_type": type(exc).__name__})
        raise HTTPException(
            503, "Rewards are temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "5"},
        ) from exc
    if not hit.allowed:
        log.info("social_rewards.claim_throttled")
        raise HTTPException(
            429, "Too many reward requests. Please retry shortly.",
            headers=rate_limit_headers(hit),
        )
