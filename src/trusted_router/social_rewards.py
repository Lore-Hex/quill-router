"""Fixed, account-scoped unverified promotional campaign; the typed grant is the receipt.

There is deliberately no mutable 'claimed' row to get ahead of a payout. An
uncertain commit is recovered by retrying the same server-generated event ID.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from trusted_router.config import Settings
from trusted_router.storage import STORE
from trusted_router.storage_models import SocialRewardEnrollment

log = logging.getLogger(__name__)

CAMPAIGN_ID = "social-rewards-v1"
SIGNUP_MICRODOLLARS = 150_000
ACTION_MICRODOLLARS = 30_000
ACTIONS = (
    ("github", "Star TrustedRouter on GitHub", "https://github.com/Lore-Hex/quill-router"),
    ("x_trustedrouter", "Follow TrustedRouter on X", "https://x.com/TrustedRouter"),
    ("x_founder", "Follow @jperla on X", "https://x.com/jperla"),
    ("linkedin", "Follow TrustedRouter on LinkedIn", "https://www.linkedin.com/company/trustedrouter"),
    ("youtube", "Subscribe to TrustedRouter on YouTube", ""),
)


def campaign_enabled(settings: Settings) -> bool:
    return bool(
        settings.social_rewards_enabled
        and settings.social_rewards_plane_id
        and settings.social_rewards_plane_id == settings.social_rewards_issuer_id
    )


def youtube_available(settings: Settings) -> bool:
    """An absent or invalid channel hides only YouTube, including direct claims."""
    try:
        youtube = urlsplit(settings.social_rewards_youtube_url)
        return (
            youtube.scheme == "https" and youtube.hostname in {"youtube.com", "www.youtube.com"}
            and youtube.username is None and youtube.password is None
            and youtube.port in {None, 443}
            and youtube.path.startswith(("/@", "/channel/"))
            and len(youtube.path.removeprefix("/@").removeprefix("/channel/")) > 0
        )
    except ValueError:
        return False


def claim_event_id(user_id: str, action: str) -> str:
    return f"{CAMPAIGN_ID}:{user_id}:{action}"


def _eligible_enrollment(user_id: str, settings: Settings) -> SocialRewardEnrollment | None:
    enrollment = STORE.get_social_reward_enrollment(user_id)
    if enrollment is None or enrollment.issuer != settings.social_rewards_plane_id:
        return None
    workspace = STORE.get_workspace(enrollment.workspace_id)
    if workspace is None or workspace.owner_user_id != user_id:
        return None
    return enrollment


def _first_call_complete(enrollment: SocialRewardEnrollment) -> bool:
    if enrollment.first_call_complete:
        return True
    # This is only a hint for the once-only popup, never claim eligibility.
    # Do not query ClickHouse or let optional attribution block receipt reads.
    try:
        attribution = STORE.get_acquisition_attribution(enrollment.workspace_id)
        successful = bool(attribution and "first_successful_api_call" in attribution.milestones)
        if successful:
            STORE.mark_social_reward_first_call(enrollment.user_id)
        return successful
    except Exception:  # noqa: BLE001 - optional presentation evidence must fail open.
        log.warning("social_rewards.first_call_status_unavailable")
        return False


def status(user_id: str, settings: Settings) -> dict[str, Any]:
    enrollment = _eligible_enrollment(user_id, settings)
    workspace = STORE.get_workspace(enrollment.workspace_id) if enrollment else None
    return {
        "eligible": enrollment is not None,
        "enabled": campaign_enabled(settings),
        "first_call_complete": bool(enrollment and _first_call_complete(enrollment)),
        "dismissed": bool(enrollment and enrollment.dismissed),
        "workspace_name": workspace.name if workspace else "",
        "actions": [
            {
                "id": action,
                "label": label,
                "url": settings.social_rewards_youtube_url if action == "youtube" else url,
                "claimed": bool(enrollment and STORE.has_credit_grant(
                    enrollment.workspace_id, claim_event_id(user_id, action)
                )),
            }
            for action, label, url in ACTIONS
            if action != "youtube" or youtube_available(settings)
        ] if enrollment else [],
    }


def require_offer(user_id: str, settings: Settings) -> SocialRewardEnrollment:
    if not campaign_enabled(settings):
        raise HTTPException(409, "Rewards are temporarily unavailable. Your progress is saved.")
    enrollment = _eligible_enrollment(user_id, settings)
    if enrollment is None:
        raise HTTPException(403, "This account is not enrolled in this offer.")
    return enrollment


def claim(user_id: str, action: str, settings: Settings) -> dict[str, Any]:
    enrollment = require_offer(user_id, settings)
    if action not in {item[0] for item in ACTIONS} or (
        action == "youtube" and not youtube_available(settings)
    ):
        raise HTTPException(422, "Unknown reward action.")
    try:
        credited = STORE.credit_workspace_once(
            enrollment.workspace_id, ACTION_MICRODOLLARS, claim_event_id(user_id, action),
            required_owner_user_id=user_id,
        )
    except ValueError as exc:
        if str(exc) != "credit_workspace_owner_mismatch":
            raise
        raise HTTPException(403, "This workspace is no longer eligible for this offer.") from exc
    if credited:
        try:
            from trusted_router.acquisition import _log_conversion

            attribution = STORE.get_acquisition_attribution(enrollment.workspace_id)
            if attribution is not None:
                _log_conversion("acquisition.social_reward_claimed", attribution,
                                extra={"reward_action": action, "verification": "none"})
        except Exception:  # noqa: BLE001 - telemetry cannot undo a committed grant.
            log.warning("social_rewards.telemetry_failed", exc_info=True)
    # This strongly reads the committed typed event, never inferring success
    # from a request, a link visit, current spendable balance, or client state.
    return status(user_id, settings)
