"""Response-shape helpers for control-plane entities.

These exist because the JSON wire shape isn't a 1:1 mapping of the dataclass —
the API returns money fields in both float-dollar and integer-microdollar
forms (for OpenRouter compat), exposes computed fields like `limit_remaining`,
and resolves cross-entity fields like a member's email."""

from __future__ import annotations

from typing import Any

from trusted_router.catalog import PROVIDERS
from trusted_router.money import microdollars_to_float
from trusted_router.spend_windows import WINDOWS, utcnow, window_resets_at
from trusted_router.storage import (
    STORE,
    ApiKey,
    ByokProviderConfig,
    CustomModel,
    Member,
    Workspace,
)
from trusted_router.storage_custom_models import custom_model_slug


def key_shape(key: ApiKey, *, window_usage: dict[str, int] | None = None) -> dict[str, Any]:
    """`window_usage` ({"daily"|"weekly"|"monthly": micro}) carries the key's
    CURRENT-window spend when the caller has it (typed point-read or the
    InMemory snapshot). Without it, the per-window fields fall back to the
    lifetime value (the pre-window OpenRouter-compat placeholder behavior)."""
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
    usage_breakdown = _windowed_money("usage", key.usage_microdollars, window_usage)
    byok_breakdown = _windowed_money("byok_usage", key.byok_usage_microdollars, None)
    # Per-window limits + remaining + reset times: what an agent holding the key
    # polls (GET /v1/key) to pace its spend. Remaining is against the current
    # window's usage when known.
    now = utcnow()
    window_fields: dict[str, Any] = {}
    for window in WINDOWS:
        limit_value = getattr(key, f"limit_{window}_microdollars", None)
        window_fields[f"limit_{window}"] = (
            None if limit_value is None else microdollars_to_float(limit_value)
        )
        window_fields[f"limit_{window}_microdollars"] = limit_value
        if limit_value is not None:
            used = (window_usage or {}).get(window)
            remaining = None if used is None else max(limit_value - used, 0)
            window_fields[f"limit_{window}_remaining"] = (
                None if remaining is None else microdollars_to_float(remaining)
            )
            window_fields[f"limit_{window}_remaining_microdollars"] = remaining
            window_fields[f"limit_{window}_resets_at"] = (
                window_resets_at(window, now).isoformat().replace("+00:00", "Z")
            )
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
        **window_fields,
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
        "secret_storage": "envelope" if config.encrypted_secret is not None else "external_ref",
        "key_hint": config.key_hint,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def custom_model_owner_shape(model: CustomModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "slug": custom_model_slug(model.id),
        "name": model.name,
        "base_model_id": model.base_model_id,
        "hidden_prompt": model.hidden_prompt,
        "revision": model.revision,
        "enabled": model.enabled,
        "owner_user_id": model.owner_user_id,
        "owner_workspace_id": model.owner_workspace_id,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def custom_model_public_shape(model: CustomModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "slug": custom_model_slug(model.id),
        "name": model.name,
        "base_model_id": model.base_model_id,
        "revision": model.revision,
        "enabled": model.enabled,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def _windowed_money(
    prefix: str, microdollars: int, window_usage: dict[str, int] | None
) -> dict[str, Any]:
    """OpenRouter-compat: every usage row is duplicated as `_daily`, `_weekly`,
    `_monthly`. With `window_usage` those carry the REAL current-window spend
    (lazy fixed-UTC windows); without it they fall back to the lifetime value
    (the historical placeholder behavior)."""
    dollars = microdollars_to_float(microdollars)
    out: dict[str, Any] = {prefix: dollars, f"{prefix}_microdollars": microdollars}
    for window in ("daily", "weekly", "monthly"):
        value = microdollars if window_usage is None else window_usage.get(window, 0)
        out[f"{prefix}_{window}"] = microdollars_to_float(value)
        out[f"{prefix}_{window}_microdollars"] = value
    return out
