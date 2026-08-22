from __future__ import annotations

from trusted_router.config import Settings


def synthetic_observer_token(settings: Settings) -> str | None:
    """Return the one credential synthetic callers are allowed to present.

    There is deliberately no fallback to the billing gateway token. Synthetic
    jobs may post observer-owned samples and remediation requests, but must
    never inherit the credential that authorizes billing gateway mutations.
    """

    return settings.observer_internal_token
