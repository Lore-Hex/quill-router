"""Parasail — human-only provider config.

**Special case**: Parasail's dashboard pricing lives behind a SaaS
login (saas.parasail.io/info/pricing) that we can't scrape, and
api.parasail.io's /v1/models endpoint doesn't include a pricing
block. Rather than hand-maintain a static price table (high
maintenance, easy to drift) OR ship without Parasail routes,
**we pull pricing from OpenRouter's snapshot as a fallback** for
any model Parasail's /v1/models reports as live.

Trade-off:
  - OR's headline price is "lowest provider on OR's list," not
    Parasail's actual rate. For most open-weight models Parasail
    hosts (Llama, Gemma, Qwen, DeepSeek) the OR baseline is
    representative of the open-market floor that Parasail also
    targets, so the over/under is usually small.
  - Worst case: TR over-bills or under-bills customers on Parasail
    routes by tens of percent. That's accepted while Parasail is a
    secondary route; the auto-router will still pick the cheapest
    provider per request, so over-billing pushes traffic AWAY from
    Parasail organically.

Once Parasail publishes a real machine-readable price feed, swap
this scraper to the API-direct pattern used by lightning.py / gmi.py
/ deepinfra.py — those parse pricing from each model's response
entry. Until then, the OR-fallback path lives here and is the only
adapter in TR that does this.
"""
from __future__ import annotations

import os
from decimal import Decimal
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

SLUG = "parasail"
URL = "https://api.parasail.io/v1/models"

EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
]


# Parasail-native id → OR-canonical id. The /v1/models endpoint
# returns both forms for many models (e.g. both
# `parasail-gemma-4-31b-it` and `google/gemma-4-31B-it`); we map
# both to the same OR-canonical entry so route lookup works
# whichever alias a customer references.
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


def _or_pricing_to_model_price(pricing: dict[str, Any] | None) -> ModelPrice | None:
    """Convert OR's headline pricing block (USD/token strings) to a
    ModelPrice in microdollars per million. Returns None on missing
    or unparseable rates."""
    if not isinstance(pricing, dict):
        return None
    try:
        prompt = Decimal(str(pricing.get("prompt") or "0"))
        completion = Decimal(str(pricing.get("completion") or "0"))
    except Exception:  # noqa: BLE001
        return None
    if prompt <= 0 or completion <= 0:
        return None
    # 1 USD/token = 1e12 micro/M
    factor = Decimal(1_000_000_000_000)
    return ModelPrice(
        prompt_micro_per_m=int((prompt * factor).to_integral_value()),
        completion_micro_per_m=int((completion * factor).to_integral_value()),
    )


def _build_or_pricing_index() -> dict[str, dict[str, Any]]:
    """Build OR's id → pricing-block dict by calling the OR ingest's
    `build_snapshot` function. Imported lazily so a unit test that
    mocks Parasail doesn't transitively need the OR HTTP fetch.
    """
    # sys.path is set up by refresh.py at orchestrator load time,
    # which is the only context this provider runs in. Importing
    # here (inside fetch()) keeps the module-load-time path quiet
    # for tests that import without sys.path tweaks.
    from ingest_openrouter_catalog import build_snapshot as build_openrouter_snapshot

    snapshot = build_openrouter_snapshot()
    return {
        m["id"]: (m.get("pricing") or {})
        for m in snapshot.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    }


def fetch() -> ProviderPricingResult:
    """Hit /v1/models to discover what Parasail actually serves, then
    look up each served OR-canonical id's pricing in OpenRouter's
    headline snapshot. Returns only models present in BOTH sets."""
    api_key = os.environ.get("PARASAIL_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    notes: list[str] = []
    live_native: set[str] = set()
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
        live_native = {
            str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")
        }
    except Exception as exc:  # noqa: BLE001
        notes.append(f"/v1/models fetch failed ({exc}); treating all known natives as live")
        live_native = set(_NATIVE_TO_OR_ID.keys())

    or_ids_live = {_NATIVE_TO_OR_ID[n] for n in live_native if n in _NATIVE_TO_OR_ID}
    if not or_ids_live:
        notes.append("no Parasail-native IDs mapped to OR-canonical IDs — extend _NATIVE_TO_OR_ID")
        return ProviderPricingResult(
            slug=SLUG, prices={}, source="api", fetched_url=URL, notes=notes,
        )

    try:
        or_pricing_by_id = _build_or_pricing_index()
    except Exception as exc:  # noqa: BLE001
        # OR fetch failures shouldn't kill Parasail; surface as a note
        # and return empty. The refresh.py orchestrator already runs
        # its own OR fetch, so the network condition will be visible
        # there too.
        notes.append(f"OR snapshot fetch failed ({exc}); Parasail returns empty")
        return ProviderPricingResult(
            slug=SLUG, prices={}, source="api", fetched_url=URL, notes=notes,
        )

    prices: dict[str, ModelPrice] = {}
    for or_id in sorted(or_ids_live):
        or_price_block = or_pricing_by_id.get(or_id)
        mp = _or_pricing_to_model_price(or_price_block)
        if mp is None:
            notes.append(f"OR has no pricing for {or_id}; Parasail endpoint dropped")
            continue
        prices[or_id] = mp

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
