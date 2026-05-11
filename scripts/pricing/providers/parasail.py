"""Parasail — human-only provider config.

Parasail's dashboard pricing lives behind a SaaS login
(saas.parasail.io/info/pricing) that the Jina-markdown scraper
can't see, and api.parasail.io's /v1/models endpoint doesn't
include a pricing block. Static `_PRICES` table below is
operator-pasted from the dashboard.

Format: OR-canonical-id → (prompt_micro_per_m, completion_micro_per_m,
prompt_cached_micro_per_m). Every row carries a trailing comment
with the date the row was pasted, the operator initials, and (when
visible) the per-MTok dollar values exactly as they appeared in
the dashboard. When you add/change a row, keep that audit trail —
the next person to look will need to know what to distrust.

When Parasail publishes a machine-readable price feed, swap this
scraper to the API-direct pattern used by lightning.py / gmi.py /
deepinfra.py.
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


# Operator-pasted rates from saas.parasail.io/info/pricing.
# Format: OR-canonical-id → (prompt_$/M, completion_$/M, cached_input_$/M | None).
# Every row carries date + operator initials + the dashboard's
# displayed per-MTok dollar values so it's auditable later.
_RATES_USD_PER_M: dict[str, tuple[float, float, float | None]] = {
    # jp 2026-05-11, parasail-gemma-4-31b-it on the dashboard
    # showed: $0.14/MTok input, $0.40/MTok output, $0.10/MTok cached.
    "google/gemma-4-31b-it": (0.14, 0.40, 0.10),
    # Other Parasail-mapped models below are still TBD — add a row
    # for each once the operator pastes the dashboard rate (and the
    # corresponding native id is in _NATIVE_TO_OR_ID below).
}


def _model_price_from_usd_per_m(
    prompt: float, completion: float, cached: float | None
) -> ModelPrice:
    """Convert per-MTok dollar values to a ModelPrice in micro/M.
    $1 = 1_000_000 micro = $1.00 per MTok = 1_000_000 micro per MTok."""
    return ModelPrice(
        prompt_micro_per_m=int(round(prompt * 1_000_000)),
        completion_micro_per_m=int(round(completion * 1_000_000)),
        prompt_cached_micro_per_m=(
            int(round(cached * 1_000_000)) if cached is not None else None
        ),
    )


def fetch() -> ProviderPricingResult:
    """Hit /v1/models for liveness, then look up each served
    OR-canonical id in `_RATES_USD_PER_M`. Returns prices only for
    models that appear in BOTH (Parasail serving it AND operator
    has pasted a rate)."""
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
    prices: dict[str, ModelPrice] = {}
    for or_id, rates in _RATES_USD_PER_M.items():
        if or_id not in or_ids_live:
            notes.append(f"have a price for {or_id} but /v1/models doesn't list it — skipped")
            continue
        prices[or_id] = _model_price_from_usd_per_m(*rates)

    # Surface models Parasail serves that we don't yet have rates
    # for so the operator notices and pastes them.
    unpriced = sorted(or_ids_live - set(_RATES_USD_PER_M.keys()))
    if unpriced:
        notes.append(
            f"Parasail serves {len(unpriced)} mapped model(s) without rates in "
            f"_RATES_USD_PER_M: {', '.join(unpriced)} — paste from dashboard to enable"
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
