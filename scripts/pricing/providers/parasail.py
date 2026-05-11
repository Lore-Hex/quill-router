"""Parasail — human-only provider config.

Parasail's public pricing page is gated behind their SaaS login
(saas.parasail.io) so the standard Jina-markdown scrape path
returns an empty page. Their API `/v1/models` only exposes
`{id, object, owned_by}` — no pricing block.

Until Parasail publishes a machine-readable price feed, we hand-
maintain a STATIC table of OR-canonical-id → ModelPrice for the
models TR routes through Parasail. The table is small (just the
gemma-4 family today plus the headline llama / qwen / deepseek
sizes Parasail serves) and updates by hand once or twice a year
when their pricing notes shift.

The rates below were taken from Parasail's published rate-sheet
on 2026-05-11 (provided to TR by Parasail). Adjust by hand when
they email a new rate sheet.

When this table goes stale, the rates will be wrong but the route
keeps serving. Worst case: TR over-bills or under-bills customers
on Parasail-routed traffic, but never blocks a request. That's an
acceptable failure mode for a small auxiliary provider.
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

# Hand-maintained price table for Parasail-keyed models. Microdollars
# per million tokens, on the lowest available tier. Source: Parasail
# rate sheet, last refreshed 2026-05-11. Add models when we extend
# TR's Parasail routing.
_PRICES: dict[str, tuple[int, int]] = {
    # (prompt_micro_per_m, completion_micro_per_m)
    "google/gemma-4-31b-it":      (140_000, 400_000),    # $0.14/M in, $0.40/M out
    "google/gemma-4-26b-a4b-it":  (130_000, 400_000),    # $0.13/M in, $0.40/M out
    "google/gemma-3-27b-it":      (100_000, 200_000),    # $0.10/M in, $0.20/M out
    "meta-llama/llama-3.3-70b-instruct": (270_000, 500_000),
    "qwen/qwen2.5-vl-72b-instruct":      (450_000, 900_000),
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
