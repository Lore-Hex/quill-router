"""Parasail — human-only provider config.

Parasail's pricing lives behind a dashboard login at
saas.parasail.io/info/pricing — Jina-markdown can't see it,
and the api.parasail.io API key doesn't unlock any pricing
endpoint (every /v1/* path TR tried returns "No static resource").

**No guessed prices.** Until pricing is reachable by the scraper,
this adapter returns ZERO prices, which means the refresh.py
merge logic drops every Parasail endpoint from the snapshot.
Parasail provider is registered in catalog.PROVIDERS (so the
slug is recognized in routes) and in
GATEWAY_PREPAID_PROVIDER_SLUGS (so the gateway WOULD authorize),
but with no priced endpoints in the snapshot, no route resolves
to Parasail. Restore by populating _PRICES with operator-pasted
data from the dashboard.

Two paths to restoring routes:
  (a) Operator pastes the dashboard rates into _PRICES below.
      Mark the source with the date pasted and a note about who
      verified them. Pricing is per-OR-canonical-id; the API id
      mapping is in _NATIVE_TO_OR_ID.
  (b) Parasail ships a public/machine-readable pricing feed.
      Replace the static table with a JSON fetch (Lightning /
      GMI / DeepInfra do this — see those adapters for the
      pattern).
"""
from __future__ import annotations

import os

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)

SLUG = "parasail"
URL = "https://api.parasail.io/v1/models"

EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
]

# Operator-pasted price table for Parasail-keyed models. Microdollars
# per million tokens, lowest tier. EMPTY by default — every row in
# here MUST be sourced from Parasail's authenticated dashboard at
# saas.parasail.io/info/pricing (or an email rate-sheet), not guessed.
#
# Format: OR-canonical-id → (prompt_micro_per_m, completion_micro_per_m).
# When you add a row, include the date and where you got the price in
# the trailing comment so the next person to audit knows what to
# distrust.
#
# While this table is empty, Parasail endpoints are dropped from the
# snapshot at merge time — refresh.py:_merge_snapshot uses the
# (model_id, provider_slug) → ModelPrice map and skips endpoints
# without a price.
_PRICES: dict[str, tuple[int, int]] = {
    # Example shape — replace with real numbers from the dashboard:
    # "google/gemma-4-31b-it": (140_000, 400_000),  # $0.14/M in, $0.40/M out (dashboard 2026-05-11, jp)
}

# Parasail-native model id → OR-canonical. The /v1/models endpoint
# returns both forms (e.g. both `parasail-gemma-4-31b-it` and
# `google/gemma-4-31B-it`). We map both to the canonical OR id
# so route lookup works regardless of which alias is referenced.
_NATIVE_TO_OR_ID = {
    "parasail-gemma-4-31b-it": "google/gemma-4-31b-it",
    "google/gemma-4-31B-it": "google/gemma-4-31b-it",
    "google/gemma-4-31b-it": "google/gemma-4-31b-it",
    "parasail-gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it",
    "google/gemma-4-26B-A4B-it": "google/gemma-4-26b-a4b-it",
    "parasail-gemma3-27b-it": "google/gemma-3-27b-it",
    "google/gemma-3-27b-it": "google/gemma-3-27b-it",
    "parasail-llama-33-70b-fp8": "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "parasail-qwen25-vl-72b-instruct": "qwen/qwen2.5-vl-72b-instruct",
    "Qwen/Qwen2.5-VL-72B-Instruct": "qwen/qwen2.5-vl-72b-instruct",
}


def fetch() -> ProviderPricingResult:
    """Hit /v1/models for liveness, then return the static price table
    intersected with the models actually exposed."""
    api_key = os.environ.get("PARASAIL_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    live_native: set[str] = set()
    notes: list[str] = []
    try:
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
        live_native = {str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")}
    except Exception as exc:  # noqa: BLE001
        notes.append(f"/v1/models fetch failed ({exc}); falling back to full static table")
        live_native = set(_NATIVE_TO_OR_ID.keys())

    # Only include OR-canonical ids that BOTH (a) have a static
    # price in _PRICES and (b) are actually served by Parasail right
    # now per /v1/models. The intersection prevents stale entries
    # surfacing as routable when Parasail rotates their catalog.
    or_ids_live = {_NATIVE_TO_OR_ID[n] for n in live_native if n in _NATIVE_TO_OR_ID}
    prices: dict[str, ModelPrice] = {}
    for or_id, (prompt, completion) in _PRICES.items():
        if or_id not in or_ids_live:
            continue
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
        )

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
