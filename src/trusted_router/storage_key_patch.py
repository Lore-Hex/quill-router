"""Backend-neutral API-key patch semantics."""

from __future__ import annotations

from typing import Any

from trusted_router.money import dollars_to_microdollars
from trusted_router.scopes import validate_api_key_scopes
from trusted_router.storage_models import ApiKey, iso_now

TYPED_LIMIT_PATCH_FIELDS = frozenset(
    {
        "limit",
        "limit_microdollars",
        "limit_daily_microdollars",
        "limit_weekly_microdollars",
        "limit_monthly_microdollars",
        "include_byok_in_limit",
    }
)


def apply_key_patch(key: ApiKey, patch: dict[str, Any]) -> None:
    if "name" in patch and patch["name"]:
        key.name = str(patch["name"])
    if "disabled" in patch:
        key.disabled = bool(patch["disabled"])
    if "limit" in patch:
        value = patch["limit"]
        key.limit_microdollars = (
            None if value is None else dollars_to_microdollars(value)
        )
    if "limit_microdollars" in patch:
        key.limit_microdollars = patch["limit_microdollars"]
    if "limit_reset" in patch:
        key.limit_reset = patch["limit_reset"]
    for window in ("daily", "weekly", "monthly"):
        field = f"limit_{window}_microdollars"
        if field in patch:
            setattr(key, field, patch[field])
    if "include_byok_in_limit" in patch:
        key.include_byok_in_limit = bool(patch["include_byok_in_limit"])
    if patch.get("budget_alert_only") is not None:
        key.budget_alert_only = bool(patch["budget_alert_only"])
    if "budget_alerted" in patch:
        key.budget_alerted = dict(patch["budget_alerted"] or {})
    if "tags" in patch:
        key.tags = dict(patch["tags"] or {})
    if "scopes" in patch:
        key.scopes = validate_api_key_scopes(
            patch["scopes"],
            management=key.management,
        )
    key.updated_at = iso_now()
