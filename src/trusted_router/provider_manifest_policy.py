"""Shared fail-closed policy for authenticated provider catalog manifests."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.pricing import provider_manifest_price_profile_is_valid

# Every provider whose live scraper can fall back to a committed manifest is
# quarantined independently when that manifest ages out. All of them have CI
# discovery access through the hourly refresh; the age limit is provider-scoped
# stale-price containment for the hours that discovery fails.
EXPIRING_PROVIDER_MANIFEST_SLUGS = frozenset(
    {
        "abliterate",
        "abliteration",
        "meta",
        "aion-labs",
        "akashml",
        "arcee",
        "inception",
        "mancer",
        "maple",
        "nextbit",
        "nextbit-eu",
        "reka",
        "sail-research",
        "sambanova",
        "streamlake",
        "upstage",
        "bfl",
        "decart",
        "fal",
        "io-net",
        "tencent",
        "krea",
        "perplexity",
        "scaleway",
        "regolo",
        "lyceum",
        "amazon-bedrock",
        "greenference",
        "hixtube",
        "muna",
        "negbeta",
        "coralbricks",
        "boundless",
        "zro",
        "byteplus",
        "privatemode",
        "featherless",
        "sakana",
        "jina",
        "near-ai",
        "confidential-ai",
        "scaledown",
        "nvidia-nim",
        "wandb",
        "nscale",
        "recraft",
        "relace",
        "stepfun",
        "decisionmodelsio",
        "decisionmodelsio-eu",
    }
)
PROVIDER_MANIFEST_MAX_AGE_DAYS = 14
EXPIRED_PROVIDER_MANIFEST = datetime.min.replace(tzinfo=UTC)
_CANARY_QUARANTINE_REASONS = frozenset({
    "provider-canary-failed", "upstream-usage-unavailable", "upstream-output-limit-unenforced",
})
DECISION_CONTRACT_HOLDS = frozenset({
    "decision-fixed-questions-unsupported",
    "decision-model-namespace-unsupported",
    "decision-image-pricing-unsupported",
    "decision-image-contract-unsupported",
})


def decision_manifest_price_is_valid(row: dict[str, Any]) -> bool:
    """Input-only decision routes must never inherit chat or cache pricing."""
    model_id = row.get("id")
    prompt = row.get("input_token_price_per_m")
    completion = row.get("output_token_price_per_m")
    hold = row.get("decision_contract_hold")
    image_price = row.get("decision_image_input_price_per_m")
    return (
        isinstance(model_id, str)
        and (
            re.fullmatch(r"decisionmodelsio(?:-eu)?/[a-z0-9]+(?:-[a-z0-9]+)*", model_id) is not None
            or (model_id == "cloudflare/clef-flash" and row.get("upstream_id") == "clef-flash")
        )
        and (
            hold is None
            or (isinstance(hold, str) and hold in DECISION_CONTRACT_HOLDS and row.get("routable") is False)
        )
        and (image_price is None or (type(image_price) is int and image_price > 0 and row.get("routable") is False))
        and row.get("model_type") == "decision"
        and row.get("endpoints") == ["decide"]
        and type(prompt) is int
        and prompt > 0
        and type(completion) is int
        and completion == 0
        and "price_tiers" not in row
        and "cached_input_token_price_per_m" not in row
    )


def request_billed_speech_price_is_valid(row: dict[str, Any]) -> bool:
    return (
        row.get("model_type") == "tts"
        and row.get("endpoints") == ["audio/speech"]
        and row.get("billing_unit") == "request"
        and type(row.get("fixed_request_price_microdollars")) is int
        and row["fixed_request_price_microdollars"] > 0
        and not {"input_token_price_per_m", "output_token_price_per_m", "cached_input_token_price_per_m", "price_tiers"}.intersection(row)
    )


def request_billed_transcription_price_is_valid(row: dict[str, Any]) -> bool:
    if row.get("model_type") != "audio" or row.get("endpoints") != ["audio/transcriptions"]:
        return False
    if row.get("billing_unit") == "tokens":
        return provider_manifest_price_profile_is_valid(row) and not {
            "fixed_request_price_microdollars", "audio_minute_price_microdollars",
        }.intersection(row)
    unit_field = {
        "request": "fixed_request_price_microdollars",
        "audio_minute": "audio_minute_price_microdollars",
    }.get(str(row.get("billing_unit")))
    if unit_field is None:
        return False
    return (
        type(row.get(unit_field)) is int
        and row[unit_field] > 0
        and not {"input_token_price_per_m", "output_token_price_per_m", "cached_input_token_price_per_m", "price_tiers"}.intersection(row)
        and not ({"fixed_request_price_microdollars", "audio_minute_price_microdollars"} - {unit_field}).intersection(row)
    )


def _provider_manifest_row_price_is_valid(row: dict[str, Any]) -> bool:
    model_type = row.get("model_type") or "chat"
    try:
        if model_type == "chat":
            return provider_manifest_price_profile_is_valid(row)
        if model_type == "decision":
            return decision_manifest_price_is_valid(row)
        if model_type == "tts":
            return request_billed_speech_price_is_valid(row)
        if model_type == "audio":
            return request_billed_transcription_price_is_valid(row)
        if model_type == "image":
            fixed = row.get("fixed_output_price_microdollars")
            return (
                isinstance(fixed, dict)
                and bool(fixed)
                and all(int(value) > 0 for value in fixed.values())
            )
        if model_type == "video":
            if row.get("billing_unit") == "output_tokens":
                return (
                    type(row.get("input_token_price_per_m")) is int
                    and row["input_token_price_per_m"] == 0
                    and type(row.get("output_token_price_per_m")) is int
                    and row["output_token_price_per_m"] > 0
                    and row.get("endpoints") == ["videos"]
                    and "price_tiers" not in row
                )
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
    deadline = generated.astimezone(UTC) + timedelta(days=max_age_days)
    # A published promotion may end before the ordinary stale-manifest limit.
    if "pricing_valid_until" in raw:
        try:
            price_deadline = datetime.fromisoformat(raw["pricing_valid_until"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return EXPIRED_PROVIDER_MANIFEST
        if price_deadline.tzinfo is None:
            return EXPIRED_PROVIDER_MANIFEST
        deadline = min(deadline, price_deadline.astimezone(UTC))
    return deadline


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
        allowed_reasons = _CANARY_QUARANTINE_REASONS
        if provider_slug in {"decisionmodelsio", "decisionmodelsio-eu"}:
            allowed_reasons |= DECISION_CONTRACT_HOLDS
        if row.get("routable_reason") not in allowed_reasons:
            return EXPIRED_PROVIDER_MANIFEST
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            return EXPIRED_PROVIDER_MANIFEST
        if not _provider_manifest_row_price_is_valid(row):
            return EXPIRED_PROVIDER_MANIFEST
    return _provider_manifest_generated_deadline(raw, max_age_days)
