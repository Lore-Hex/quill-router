from __future__ import annotations

from trusted_router.config import Settings


def synthetic_observer_token(settings: Settings) -> str | None:
    """Return the one credential synthetic callers are allowed to present.

    There is deliberately no fallback to the billing gateway token. Synthetic
    jobs may post observer-owned samples and remediation requests, but must
    never inherit the credential that authorizes billing gateway mutations.
    The explicit combined migration bridge is the one bounded exception: its
    already-deployed pre-#714 jobs and server share that legacy credential
    until the split rollout in #712 replaces both ends together.
    """

    if (
        settings.service_surface == "combined"
        and settings.allow_deployed_combined_surface
    ):
        return settings.internal_gateway_token
    return settings.observer_internal_token
