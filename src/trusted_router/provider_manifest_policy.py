"""Shared fail-closed policy for authenticated provider catalog manifests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.pricing import provider_manifest_price_profile_is_valid

RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS = frozenset(
    {
        "aion-labs",
        "akashml",
        "arcee",
        "inception",
        "mancer",
        "nscale",
        "nextbit",
        "reka",
        "sail-research",
        "sambanova",
        "upstage",
    }
)

# Every provider whose live scraper can fall back to a committed manifest is
# quarantined independently when that manifest ages out. Runtime-only is the
# stricter credential-isolation subset; the remaining entries already have CI
# discovery access but still need provider-scoped stale-price containment.
EXPIRING_PROVIDER_MANIFEST_SLUGS = RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS | frozenset(
    {
        "bfl",
        "decart",
        "fal",
        "io-net",
        "krea",
        "perplexity",
        "scaleway",
        "featherless",
        "sakana",
        "jina",
        "near-ai",
        "nvidia-nim",
        "wandb",
        "nscale",
        "recraft",
        "relace",
        "stepfun",
    }
)
PROVIDER_MANIFEST_MAX_AGE_DAYS = 14
RUNTIME_ONLY_PROVIDER_MANIFEST_MAX_AGE_DAYS = PROVIDER_MANIFEST_MAX_AGE_DAYS
EXPIRED_PROVIDER_MANIFEST = datetime.min.replace(tzinfo=UTC)
_CANARY_QUARANTINE_REASONS = frozenset({"provider-canary-failed"})


def _provider_manifest_row_price_is_valid(row: dict[str, Any]) -> bool:
    model_type = row.get("model_type") or "chat"
    try:
        if model_type == "chat":
            return provider_manifest_price_profile_is_valid(row)
        if model_type == "image":
            fixed = row.get("fixed_output_price_microdollars")
            return (
                isinstance(fixed, dict)
                and bool(fixed)
                and all(int(value) > 0 for value in fixed.values())
            )
        if model_type == "video":
            return int(row.get("fixed_output_price_per_second_microdollars") or 0) > 0
        if model_type == "embedding":
            prompt = int(row.get("input_token_price_per_m") or 0)
            completion = int(row.get("output_token_price_per_m") or 0)
            return prompt > 0 and completion == 0
    except (TypeError, ValueError):
        return False
    return False


def _provider_manifest_generated_deadline(raw: dict[str, Any], max_age_days: int) -> datetime:
    generated_at = raw.get("generated_at")
    if not isinstance(generated_at, str):
        return EXPIRED_PROVIDER_MANIFEST
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return EXPIRED_PROVIDER_MANIFEST
    if generated.tzinfo is None:
        return EXPIRED_PROVIDER_MANIFEST
    return generated.astimezone(UTC) + timedelta(days=max_age_days)


def provider_manifest_valid_until(
    provider_slug: str,
    raw: Any,
    *,
    max_age_days: int = PROVIDER_MANIFEST_MAX_AGE_DAYS,
) -> datetime | None:
    """Return the shared runtime and audit deadline for a provider manifest."""

    if provider_slug not in EXPIRING_PROVIDER_MANIFEST_SLUGS:
        return None
    if not isinstance(raw, dict):
        return EXPIRED_PROVIDER_MANIFEST
    rows = raw.get("models")
    if not isinstance(rows, list) or not rows:
        return EXPIRED_PROVIDER_MANIFEST
    usable_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            return EXPIRED_PROVIDER_MANIFEST
        if not row.get("routable", True):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            return EXPIRED_PROVIDER_MANIFEST
        if not _provider_manifest_row_price_is_valid(row):
            return EXPIRED_PROVIDER_MANIFEST
        usable_rows += 1
    if usable_rows == 0:
        return EXPIRED_PROVIDER_MANIFEST
    return _provider_manifest_generated_deadline(raw, max_age_days)


def provider_manifest_canary_quarantine_valid_until(
    provider_slug: str,
    raw: Any,
    *,
    max_age_days: int = PROVIDER_MANIFEST_MAX_AGE_DAYS,
) -> datetime | None:
    """Return the deadline for a fresh manifest whose routes all failed canaries.

    This does not make any route usable. It lets audits distinguish an explicit,
    fail-closed provider quarantine from malformed or unpriced route metadata.
    """

    if provider_slug not in EXPIRING_PROVIDER_MANIFEST_SLUGS:
        return None
    if not isinstance(raw, dict):
        return EXPIRED_PROVIDER_MANIFEST
    rows = raw.get("models")
    if not isinstance(rows, list) or not rows:
        return EXPIRED_PROVIDER_MANIFEST
    for row in rows:
        if not isinstance(row, dict):
            return EXPIRED_PROVIDER_MANIFEST
        if row.get("routable") is not False:
            return EXPIRED_PROVIDER_MANIFEST
        if row.get("routable_reason") not in _CANARY_QUARANTINE_REASONS:
            return EXPIRED_PROVIDER_MANIFEST
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            return EXPIRED_PROVIDER_MANIFEST
        if not _provider_manifest_row_price_is_valid(row):
            return EXPIRED_PROVIDER_MANIFEST
    return _provider_manifest_generated_deadline(raw, max_age_days)
