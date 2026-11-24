"""Backend-neutral API-key patch semantics."""

from __future__ import annotations

import copy
from typing import Any

from trusted_router.money import dollars_to_microdollars
from trusted_router.scopes import validate_api_key_scopes
from trusted_router.storage_models import ApiKey, iso_now

TYPED_LIMIT_PATCH_FIELDS = frozenset(
    {
        "limit",
        "limit_microdollars",
        "limit_microdollars_at_most",
        "limit_daily_microdollars",
        "limit_weekly_microdollars",
        "limit_monthly_microdollars",
        "include_byok_in_limit",
    }
)

#: A key's spend limits: the lifetime limit and the three UTC windows. They
#: are mirrored into tr_key_limit as limit_micro and <w>_limit_micro.
KEY_LIMIT_FIELDS = (
    "limit_microdollars",
    "limit_daily_microdollars",
    "limit_weekly_microdollars",
    "limit_monthly_microdollars",
)

STRICT_FIRST_LIMIT_MESSAGE = (
    "A strict-budget key with no spend limit cannot gain one. Create a new strict key "
    "with the limits instead. A strict key's existing limits can be changed or removed, "
    "but once its last limit is removed it can never have a limit again."
)


class StrictKeyFirstLimit(ValueError):
    """A patch would give a limitless budget_strict key its first spend limit.

    So a strict key's limit fields go from some set to some set, or to none
    set, and never from none set to some set (docs/routing-controls-20260927.md,
    "Strict spending"). A strict key with no limit is one Python can never
    refuse for its budget; one that could gain a limit later would need every
    hold admitted before the limit counted against it, which a hold admitted
    without a key counter (a lease's) is not.
    """

    def __init__(self) -> None:
        super().__init__(STRICT_FIRST_LIMIT_MESSAGE)


def has_spend_limit(key: ApiKey) -> bool:
    """Whether the key has a lifetime or a window limit (KEY_LIMIT_FIELDS),
    read by name: the request path counts dynamic attribute reads."""
    return (
        key.limit_microdollars is not None
        or key.limit_daily_microdollars is not None
        or key.limit_weekly_microdollars is not None
        or key.limit_monthly_microdollars is not None
    )


def apply_key_patch(key: ApiKey, patch: dict[str, Any]) -> None:
    """Apply ``patch`` to ``key`` in place, or raise and leave it untouched.

    Every backend calls this on the key it read inside its write transaction
    (Spanner, Postgres) or under its lock (memory), so the strict-key rule
    below is checked against the stored key, never a stale copy.
    """
    if key.budget_strict and not has_spend_limit(key):
        trial = copy.deepcopy(key)
        _apply(trial, patch)
        if has_spend_limit(trial):
            raise StrictKeyFirstLimit()
    _apply(key, patch)


def _apply(key: ApiKey, patch: dict[str, Any]) -> None:
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
    if patch.get("limit_microdollars_at_most") is not None:
        # Lower the lifetime limit to at most this, never raise it. Every
        # backend applies a patch to the key it read inside its write
        # transaction (Spanner and Postgres) or under its lock (memory), so a
        # concurrent, lower cut cannot be overwritten by a higher one.
        ceiling = int(patch["limit_microdollars_at_most"])
        current = key.limit_microdollars
        key.limit_microdollars = ceiling if current is None else min(current, ceiling)
    if "limit_reset" in patch:
        key.limit_reset = patch["limit_reset"]
    for window in ("daily", "weekly", "monthly"):
        field = f"limit_{window}_microdollars"
        if field in patch:
            setattr(key, field, patch[field])
    if "include_byok_in_limit" in patch:
        key.include_byok_in_limit = bool(patch["include_byok_in_limit"])
    if patch.get("lenient") is not None:
        key.lenient = bool(patch["lenient"])
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
