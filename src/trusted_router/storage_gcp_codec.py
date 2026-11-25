from __future__ import annotations

import datetime as dt
from typing import Any

from trusted_router.storage_codec import json_body
from trusted_router.storage_models import Generation

# ``json_body`` is a plain deterministic JSON dumper, not a GCP concern, so it
# now lives in the backend-neutral :mod:`trusted_router.storage_codec`.  It is
# re-exported here (rather than moved outright) so the twelve existing
# ``from trusted_router.storage_gcp_codec import json_body`` sites keep working
# unchanged.  Everything else below really is a Spanner key shape.
__all__ = [
    "byok_id",
    "generation_workspace_id",
    "json_body",
    "member_id",
    "normalize_email",
    "reverse_time_key",
    "workspace_key_id",
]


def member_id(workspace_id: str, user_id: str) -> str:
    return f"{workspace_id}#{user_id}"


def byok_id(workspace_id: str, provider: str) -> str:
    return f"{workspace_id}#{provider}"


def workspace_key_id(workspace_id: str, key_hash: str) -> str:
    return f"{workspace_id}#{key_hash}"


def generation_workspace_id(generation: Generation) -> str:
    return f"{generation.workspace_id}#{generation.created_at[:10]}#{generation.created_at}#{generation.id}"


def reverse_time_key(created_at: str) -> str:
    parsed = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    epoch_ms = int(parsed.timestamp() * 1000)
    return f"{9_999_999_999_999 - epoch_ms:013d}"


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if "@" not in normalized:
        normalized = f"{normalized}@trustedrouter.local"
    return normalized


#: Entity kind of the per-email-domain starter-credit counter.
SIGNUP_CREDIT_DOMAIN_KIND = "signup_credit_domain"

#: Entity kind of a client session claimed by a never-funded workspace
#: (services/starter_session_gate.py). Keyed by a digest of the session id.
STARTER_SESSION_KIND = "starter_session"


def starter_session_holder(
    claim: dict[str, Any] | None, workspace_id: str, *, now: int, window_seconds: int,
) -> str | None:
    """The other workspace holding a live claim, or None when this one may claim."""
    if not claim:
        return None
    holder = str(claim.get("workspace_id") or "")
    try:
        claimed_at = int(claim.get("at") or 0)
    except (TypeError, ValueError):
        claimed_at = 0
    if holder and holder != workspace_id and now - claimed_at <= window_seconds:
        return holder
    return None


def starter_session_needs_write(claim: dict[str, Any] | None, workspace_id: str) -> bool:
    """Whether this workspace's claim must be written (absent, expired or another's)."""
    return not claim or str(claim.get("workspace_id") or "") != workspace_id


def email_domain(normalized_email: str) -> str:
    """Domain part of an already-normalized address."""
    return normalized_email.rsplit("@", 1)[-1]
