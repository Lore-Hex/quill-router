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
        model_type = row.get("model_type") or "chat"
        try:
            if model_type == "chat":
                valid_price = provider_manifest_price_profile_is_valid(row)
            elif model_type == "image":
                fixed = row.get("fixed_output_price_microdollars")
                valid_price = (
                    isinstance(fixed, dict)
                    and bool(fixed)
                    and all(int(value) > 0 for value in fixed.values())
                )
            elif model_type == "video":
                valid_price = int(row.get("fixed_output_price_per_second_microdollars") or 0) > 0
            elif model_type == "embedding":
                prompt = int(row.get("input_token_price_per_m") or 0)
                completion = int(row.get("output_token_price_per_m") or 0)
                valid_price = prompt > 0 and completion == 0
            else:
                valid_price = False
        except (TypeError, ValueError):
            return EXPIRED_PROVIDER_MANIFEST
        if not valid_price:
            return EXPIRED_PROVIDER_MANIFEST
        usable_rows += 1
    if usable_rows == 0:
        return EXPIRED_PROVIDER_MANIFEST
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
