"""API-key scope vocabulary and validation.

An empty scope list is the legacy credential shape and deliberately retains
the full pre-scope behavior. Non-empty lists describe delegated keys and are
enforced fail-closed at the API's scope-aware chokepoints.
"""

from __future__ import annotations

from collections.abc import Iterable

SCOPE_INFERENCE = "inference"
SCOPE_PROFILE = "profile"
SCOPE_BALANCE_READ = "balance:read"
SCOPE_ACTIVITY_READ = "activity:read"

KNOWN_SCOPES = frozenset(
    {
        SCOPE_INFERENCE,
        SCOPE_PROFILE,
        SCOPE_BALANCE_READ,
        SCOPE_ACTIVITY_READ,
    }
)

DEFAULT_DELEGATED_SCOPES = [
    SCOPE_INFERENCE,
    SCOPE_PROFILE,
    SCOPE_BALANCE_READ,
]


def validate_api_key_scopes(
    scopes: Iterable[str] | None,
    *,
    management: bool,
) -> list[str]:
    """Return a stored scope list or reject an invalid key shape."""
    normalized = list(scopes or [])
    unknown = sorted(set(normalized) - KNOWN_SCOPES)
    if unknown:
        noun = "scope" if len(unknown) == 1 else "scopes"
        raise ValueError(f"Unknown API key {noun}: {', '.join(unknown)}")
    if management and normalized:
        raise ValueError("Management API keys cannot have scopes")
    return normalized
