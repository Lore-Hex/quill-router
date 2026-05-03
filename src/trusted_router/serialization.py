"""Response-shape helpers for control-plane entities.

These exist because the JSON wire shape isn't a 1:1 mapping of the dataclass —
the API returns money fields in both float-dollar and integer-microdollar
forms (for OpenRouter compat), exposes computed fields like `limit_remaining`,
and resolves cross-entity fields like a member's email."""

from __future__ import annotations

from typing import Any

from trusted_router.catalog import PROVIDERS
from trusted_router.money import microdollars_to_float
from trusted_router.storage import STORE, ApiKey, ByokProviderConfig, Member, Workspace


def key_shape(key: ApiKey) -> dict[str, Any]:
    limit_microdollars = key.limit_microdollars
    has_limit = limit_microdollars is not None
    limit_used = key.usage_microdollars + (
        key.byok_usage_microdollars if key.include_byok_in_limit else 0
    )
    limit_remaining_microdollars = (
        max((limit_microdollars or 0) - limit_used - key.reserved_microdollars, 0)
        if has_limit
        else None
    )
    usage_breakdown = _windowed_money("usage", key.usage_microdollars)
    byok_breakdown = _windowed_money("byok_usage", key.byok_usage_microdollars)
    return {
        "hash": key.hash,
        "name": key.name,
        "label": key.label,
        "disabled": key.disabled,
        "limit": None if not has_limit else microdollars_to_float(limit_microdollars or 0),
        "limit_microdollars": limit_microdollars,
        "limit_remaining": (
            None
            if limit_remaining_microdollars is None
            else microdollars_to_float(limit_remaining_microdollars)
        ),
        "limit_remaining_microdollars": limit_remaining_microdollars,
        "limit_reset": key.limit_reset,
        "include_byok_in_limit": key.include_byok_in_limit,
        **usage_breakdown,
        **byok_breakdown,
        "reserved_microdollars": key.reserved_microdollars,
        "created_at": key.created_at,
        "updated_at": key.updated_at,
        "expires_at": key.expires_at,
        "creator_user_id": key.creator_user_id,
        "workspace_id": key.workspace_id,
        "management": key.management,
    }


def workspace_shape(workspace: Workspace) -> dict[str, Any]:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "owner_user_id": workspace.owner_user_id,
        "created_at": workspace.created_at,
        "content_storage_enabled": workspace.content_storage_enabled,
    }


def member_shape(member: Member) -> dict[str, Any]:
    user = STORE.get_user(member.user_id)
    return {
        "workspace_id": member.workspace_id,
        "user_id": member.user_id,
        "email": None if user is None else user.email,
        "role": member.role,
        "created_at": member.created_at,
    }


def byok_provider_shape(config: ByokProviderConfig) -> dict[str, Any]:
    return {
        "provider": config.provider,
        "provider_name": PROVIDERS[config.provider].name,
        "configured": True,
        "secret_ref": config.secret_ref,
        "key_hint": config.key_hint,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _windowed_money(prefix: str, microdollars: int) -> dict[str, Any]:
    """OpenRouter-compat: every usage row is duplicated as `_daily`,
    `_weekly`, `_monthly` (currently all the same lifetime value because
    real per-window aggregation isn't wired yet)."""
    dollars = microdollars_to_float(microdollars)
    out: dict[str, Any] = {prefix: dollars, f"{prefix}_microdollars": microdollars}
    for window in ("daily", "weekly", "monthly"):
        out[f"{prefix}_{window}"] = dollars
        out[f"{prefix}_{window}_microdollars"] = microdollars
    return out
