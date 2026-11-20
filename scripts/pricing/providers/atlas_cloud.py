"""Atlas Cloud OpenAI-compatible text model catalog and pricing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    PriceTier,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.openai_catalog import (
    discover_openai_chat_catalog,
    dollars_per_token_to_micro_per_m,
    openai_model_price,
    positive_int,
)

SLUG = "atlas-cloud"
URL = "https://api.atlascloud.ai/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "atlas-cloud.json"
)
EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "deepseek/deepseek-v4-flash",
    "minimax/minimax-m3",
]
_NATIVE_TO_MODEL_ID = {
    "deepseek-ai/deepseek-v3.2": "deepseek/deepseek-v3.2",
    "deepseek-ai/deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "minimaxai/minimax-m2.5": "minimax/minimax-m2.5",
    "minimaxai/minimax-m2.7": "minimax/minimax-m2.7",
    "minimaxai/minimax-m3": "minimax/minimax-m3",
    "moonshotai/kimi-k2.5": "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2.6": "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7-code": "moonshotai/kimi-k2.7-code",
    "zai-org/glm-4.7": "z-ai/glm-4.7",
    "zai-org/glm-5": "z-ai/glm-5",
    "zai-org/glm-5.1": "z-ai/glm-5.1",
    "zai-org/glm-5.2": "z-ai/glm-5.2",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-2507",
}
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


# Atlas's /v1/models rows carry long-context rates as
#   "pricing": {"prompt": ..., "completion": ..., "input_cache_read": ...,
#               "overrides": [{"prompt": ..., "completion": ...,
#                              "input_cache_read": ..., "min_context": 272000}]}
# On 2026-11-19, 56 rows had exactly one override (thresholds from 32768 to
# 272000, for example 200000 on Claude and 272000 on GPT 6). Atlas's own
# pricing page shows the same thresholds as
# "input≤{threshold}k" for the base rate and "{threshold}k<input" for the
# override, so the base tier covers prompts up to and including min_context.
_OVERRIDE_FIELDS = frozenset({"prompt", "completion", "input_cache_read", "min_context"})
# The base cache-read field names openai_model_price accepts, in its order.
_BASE_CACHE_FIELDS = ("input_cache_read", "input_cache_reads", "cache_read")


def atlas_model_price(row: dict[str, Any]) -> ModelPrice | None:
    """The row's flat rate, plus its long-context tier when Atlas publishes one.

    Fails closed (RuntimeError, which holds the provider) on any override shape
    not seen and reviewed: more than one override, an unknown field, a missing
    or non-positive threshold, a missing price, cached input on only one tier,
    or a long-context rate below the base rate.
    """

    base = openai_model_price(row)
    pricing = row.get("pricing")
    overrides = pricing.get("overrides") if isinstance(pricing, dict) else None
    if overrides in (None, []):
        return base
    model = row.get("id")
    if base is None:
        # Returning None would drop a still-listed model and, on the next
        # refresh, tombstone it; a tiered row with no valid base is unreviewed.
        raise RuntimeError(f"atlas-cloud: {model}: pricing override without a valid base rate")
    if not isinstance(overrides, list) or len(overrides) != 1 or not isinstance(overrides[0], dict):
        raise RuntimeError(f"atlas-cloud: {model}: expected exactly one pricing override")
    override = overrides[0]
    unknown = sorted(set(override) - _OVERRIDE_FIELDS)
    if unknown:
        raise RuntimeError(f"atlas-cloud: {model}: unreviewed pricing override field(s) {unknown}")
    threshold = positive_int(override.get("min_context"))
    prompt = dollars_per_token_to_micro_per_m(override.get("prompt"))
    completion = dollars_per_token_to_micro_per_m(override.get("completion"))
    if threshold is None or prompt is None or completion is None:
        raise RuntimeError(f"atlas-cloud: {model}: incomplete pricing override")
    has_cached = override.get("input_cache_read") is not None
    cached = dollars_per_token_to_micro_per_m(override.get("input_cache_read")) if has_cached else None
    flat = base.tiers[0]
    if flat.prompt_micro_per_m == 0 or flat.completion_micro_per_m == 0:
        # Discovery drops an all-zero headline as free, and catalog ingestion
        # refuses a tier with a zero token price, so either would silently
        # remove a route whose long-context tier is paid.
        raise RuntimeError(f"atlas-cloud: {model}: zero base rate with a paid override")
    # Read the base cache rate from the same fields the shared parser accepts,
    # but strictly: its fallback chain turns an invalid value, or a numeric 0,
    # into "absent". More than one of them is ambiguous.
    cache_fields = [field for field in _BASE_CACHE_FIELDS if pricing.get(field) is not None]
    if len(cache_fields) > 1:
        raise RuntimeError(f"atlas-cloud: {model}: more than one base cache rate {cache_fields}")
    base_cached: int | None = None
    if cache_fields:
        base_cached = dollars_per_token_to_micro_per_m(pricing[cache_fields[0]])
        if base_cached is None:
            raise RuntimeError(f"atlas-cloud: {model}: invalid cached-input base rate")
    if has_cached and cached is None:
        raise RuntimeError(f"atlas-cloud: {model}: invalid cached-input override")
    if (cached is None) != (base_cached is None):
        raise RuntimeError(f"atlas-cloud: {model}: cached input priced on only one tier")
    if prompt < flat.prompt_micro_per_m or completion < flat.completion_micro_per_m or (
        cached is not None and base_cached is not None and cached < base_cached
    ):
        raise RuntimeError(f"atlas-cloud: {model}: long-context rate below the base rate")
    return ModelPrice(tiers=[
        PriceTier(
            max_prompt_tokens=threshold,
            prompt_micro_per_m=flat.prompt_micro_per_m,
            completion_micro_per_m=flat.completion_micro_per_m,
            prompt_cached_micro_per_m=base_cached,
        ),
        PriceTier(
            max_prompt_tokens=None,
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        ),
    ])


def _supports_chat_completions(row: dict[str, Any]) -> bool:
    """Exclude Atlas image generators that its feed mislabels as text output."""

    model_id = row.get("id")
    return isinstance(model_id, str) and "image" not in model_id.casefold()


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("ATLAS_CLOUD_API_KEY")
    if not api_key:
        raise RuntimeError("ATLAS_CLOUD_API_KEY is required for model discovery")
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(
            URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": PROVIDER_FETCH_UA,
            },
        )
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    source_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    prices, discovered = discover_openai_chat_catalog(
        source_rows,
        explicit_map=_NATIVE_TO_MODEL_ID,
        upstream_id_map=UPSTREAM_ID_MAP,
        include=_supports_chat_completions,
        price_of=atlas_model_price,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[f"discovered {len(discovered)} priced text-chat models"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
