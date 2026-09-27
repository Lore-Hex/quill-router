"""Lightning AI — human-only provider config.

Lightning publishes per-model pricing in its `/v1/models` response
(under each entry's `pricing` block with `input_cost_per_token` +
`output_cost_per_token` as USD/token). API-direct path; no HTML
scraping, no LLM self-heal.

Auth: Bearer token in `LIGHTNING_API_KEY`. Without it, returns 401
and Lightning is one failure under MAX_TOLERATED_FAILURES — every
other provider still refreshes normally.

OR-canonical model id mapping is small today (just gemma-4 +
llama-3.3 to start). Extend `_NATIVE_TO_OR_ID` when we add more
Lightning-keyed models to the catalog.

Cached-input rate: Lightning's /v1/models response does NOT include
a cache-read discount field (only `input_cost_per_token` +
`output_cost_per_token`). If they add one — e.g. a
`cached_input_cost_per_token` sibling — extend the loop to read it
into `ModelPrice.prompt_cached_micro_per_m`. Today we leave it
unset, which TR's gateway treats as "upstream charges full rate on
cache hits."
"""
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
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import positive_int

SLUG = "lightning"
URL = "https://lightning.ai/api/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "lightning.json"
)

EXPECTED_MODELS = [
    # gemma-4 is the headline model in this batch; if the parser
    # ever produces zero gemma-4 prices we want validation to flag it.
    "google/gemma-4-31b-it",
]


# Lightning native ids → OR-canonical. Lightning prefixes their hosted
# variants with `lightning-ai/` (e.g. `lightning-ai/gemma-4-31B-it`,
# `lightning-ai/llama-3.3-70b`). The OR-canonical form drops the prefix
# and lowercases the size token.
_NATIVE_TO_OR_ID = {
    "lightning-ai/gemma-4-31B-it": "google/gemma-4-31b-it",
    "lightning-ai/gemma-4-26B-A4B-it": "google/gemma-4-26b-a4b-it",
    "lightning-ai/llama-3.3-70b": "meta-llama/llama-3.3-70b-instruct",
    "lightning-ai/DeepSeek-V3.1": "deepseek/deepseek-v3.1",
}
UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("LIGHTNING_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("data") or []
    # The public catalog also lists third-party apps under the id of the model
    # they wrap, each with its own name, context and sometimes price, and
    # nothing marks which listing is the model's own. So a native id listed
    # more than once never names the model, advertises the smallest context
    # any of its listings claims, and publishes no price when its listings
    # disagree on one.
    listing_count: dict[str, int] = {}
    listed_contexts: dict[str, list[int]] = {}
    listed_rates: dict[str, set[tuple[int, int]]] = {}
    latest: dict[str, tuple[str, dict[str, Any], int, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        listing_count[native_id] = listing_count.get(native_id, 0) + 1
        context_length = positive_int(row.get("context_length"))
        if context_length is not None:
            listed_contexts.setdefault(native_id, []).append(context_length)
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        # Lightning encodes prices as USD/token; convert to micro/M
        # (1 USD/token = 1e12 micro/M, so 1.4e-7 USD/token = 1.4e5
        # micro/M = $0.14/M).
        try:
            prompt_per_token = float(pricing.get("input_cost_per_token") or 0)
            completion_per_token = float(pricing.get("output_cost_per_token") or 0)
        except (TypeError, ValueError):
            continue
        if prompt_per_token <= 0 or completion_per_token <= 0:
            continue
        prompt_micro_per_m = int(round(prompt_per_token * 1_000_000_000_000))
        completion_micro_per_m = int(round(completion_per_token * 1_000_000_000_000))
        listed_rates.setdefault(native_id, set()).add((prompt_micro_per_m, completion_micro_per_m))
        # As before, a model's last priced listing picks its upstream id.
        latest[or_id] = (native_id, row, prompt_micro_per_m, completion_micro_per_m)

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for or_id, (native_id, row, prompt_micro_per_m, completion_micro_per_m) in latest.items():
        discovered_row: dict[str, Any] = {
            "id": or_id,
            "upstream_id": native_id,
            "endpoints": ["chat/completions"],
        }
        if listing_count[native_id] == 1:
            discovered_row["display_name"] = str(row.get("name") or native_id)
        if native_id in listed_contexts:
            discovered_row["context_length"] = min(listed_contexts[native_id])
        discovered[or_id] = discovered_row
        if len(listed_rates[native_id]) > 1:
            # The shared manifest writer marks a present, unpriced route
            # price-unavailable instead of billing any one listing's price.
            notes.append(
                f"{or_id}: {listing_count[native_id]} listings of {native_id} "
                "disagree on price; no price published"
            )
            continue
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt_micro_per_m,
            completion_micro_per_m=completion_micro_per_m,
        )

    _DISCOVERED_MANIFEST_ROWS = discovered

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
