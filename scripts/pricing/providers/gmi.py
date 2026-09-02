"""GMI Cloud authenticated discovery plus official billing prices.

GMI's authenticated ``/v1/models`` response is useful account metadata, but
it is not a complete availability authority: in August 2026 it temporarily
omitted GLM 5.2 while the route remained callable and listed in GMI's public
model hub. Integer list prices come from the public billing API used by GMI's
own console. That feed also carries temporary promotional prices. TrustedRouter
uses the greater of the promotional and origin list prices so an expiring
discount can never make a prepaid route sell below cost. A verified prepaid
route omitted by ``/v1/models`` is kept only after an exact paid-path PONG
canary succeeds.

API-direct, no HTML scraping, no LLM self-heal. Auth: Bearer token in
``GMI_API_KEY``. Without it, discovery fails closed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    PriceTier,
    ProviderPricingResult,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import positive_int, probe_openai_chat

SLUG = "gmi"
URL = "https://api.gmi-serving.com/v1/models"
BASE_URL = "https://api.gmi-serving.com/v1"
PRICE_URL = "https://console.gmicloud.ai/api/v1/billing/model_prices"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "gmi.json"
)

EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
]

# These are the only account routes TrustedRouter offers as GMI Credits
# routes. The broader GMI catalog remains visible for BYOK, but its model list
# has historically contained models that were not callable on our account.
_VERIFIED_PREPAID_MODELS = frozenset(
    {
        "deepseek/deepseek-v4-pro",
        "moonshotai/kimi-k3",
        "tencent/hy4-preview",
        "z-ai/glm-5",
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",
    }
)


# GMI native ids → OR-canonical. GMI mostly serves under standard
# author/model paths already (e.g. `google/gemma-4-31b-it`,
# `deepseek-ai/DeepSeek-V4-Pro`), so this map is mostly identity
# transforms with a few normalizations.
_NATIVE_TO_OR_ID = {
    "google/gemma-4-31b-it": "google/gemma-4-31b-it",
    "google/gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "deepseek-ai/DeepSeek-V3.1": "deepseek/deepseek-v3.1",
    "moonshotai/kimi-k3": "moonshotai/kimi-k3",
    "tencent/hy4-preview": "tencent/hy4-preview",
    "zai-org/GLM-5-FP8": "z-ai/glm-5",
    "zai-org/GLM-5.1-FP8": "z-ai/glm-5.1",
    "zai-org/GLM-5.2-FP8": "z-ai/glm-5.2",
    "anthropic/claude-opus-4.7": "anthropic/claude-opus-4.7",
    "openai/gpt-5.4-nano": "openai/gpt-5.4-nano",
    "openai/gpt-5.5": "openai/gpt-5.5",
}
UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _list_price(raw_tier: dict[str, Any], current_key: str, origin_key: str) -> int | None:
    """Return the conservative provider list price for one billing dimension."""

    current = _nonnegative_int(raw_tier.get(current_key))
    origin = _nonnegative_int(raw_tier.get(origin_key))
    candidates = [value for value in (current, origin) if value is not None]
    return max(candidates) if candidates else None


def _price_from_billing_row(row: dict[str, Any]) -> ModelPrice | None:
    """Parse GMI's integer microdollars-per-million list-price contract."""

    raw_tiers = row.get("tiers")
    if isinstance(raw_tiers, list) and raw_tiers:
        parsed: list[tuple[int, int, int, int | None]] = []
        for raw_tier in raw_tiers:
            if not isinstance(raw_tier, dict):
                return None
            threshold = _nonnegative_int(raw_tier.get("threshold"))
            prompt = _list_price(raw_tier, "inputPrice", "originInputPrice")
            completion = _list_price(raw_tier, "outputPrice", "originOutputPrice")
            cached = _list_price(raw_tier, "cacheReadPrice", "originCacheReadPrice")
            if threshold is None or prompt is None or completion is None:
                return None
            if prompt <= 0 or completion <= 0:
                return None
            parsed.append((threshold, prompt, completion, cached or None))
        parsed.sort(key=lambda item: item[0])
        if parsed[0][0] != 0 or len({item[0] for item in parsed}) != len(parsed):
            return None
        tiers = [
            PriceTier(
                max_prompt_tokens=(parsed[index + 1][0] - 1 if index + 1 < len(parsed) else None),
                prompt_micro_per_m=prompt,
                completion_micro_per_m=completion,
                prompt_cached_micro_per_m=cached,
            )
            for index, (_threshold, prompt, completion, cached) in enumerate(parsed)
        ]
        return ModelPrice(tiers=tiers)

    prompt = _list_price(
        row,
        "pricePer1mPromptToken",
        "originPricePer1mPromptToken",
    )
    completion = _list_price(
        row,
        "pricePer1mCompletionToken",
        "originPricePer1mCompletionToken",
    )
    if prompt is None or completion is None or prompt <= 0 or completion <= 0:
        return None
    return ModelPrice(prompt_micro_per_m=prompt, completion_micro_per_m=completion)


def _official_prices(payload: object) -> dict[str, ModelPrice]:
    raw_rows = payload.get("modelPrices") if isinstance(payload, dict) else None
    rows = raw_rows if isinstance(raw_rows, list) else []
    prices: dict[str, ModelPrice] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("billingType") != "llm":
            continue
        native_id = row.get("modelName")
        if not isinstance(native_id, str):
            continue
        model_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        price = _price_from_billing_row(row)
        if model_id is None or price is None:
            continue
        prices[model_id] = price
    return prices


def _existing_manifest_rows() -> dict[str, dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    raw_rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list):
        return {}
    return {
        row["id"]: row
        for row in raw_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def _recovered_discovery_row(model_id: str, native_id: str) -> dict[str, Any]:
    existing = _existing_manifest_rows().get(model_id, {})
    row: dict[str, Any] = {
        "id": model_id,
        "upstream_id": native_id,
        "display_name": str(existing.get("display_name") or native_id),
        "endpoints": ["chat/completions"],
    }
    for field in (
        "context_length",
        "max_output_tokens",
        "input_modalities",
        "output_modalities",
        "features",
    ):
        if field in existing:
            row[field] = existing[field]
    return row


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("GMI_API_KEY")
    if not api_key:
        raise RuntimeError("GMI_API_KEY is required for model discovery")
    catalog_payload = fetch_json(
        URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    price_payload = fetch_json(PRICE_URL)
    available_prices = _official_prices(price_payload)
    raw_rows = catalog_payload.get("data") if isinstance(catalog_payload, dict) else None
    rows = raw_rows if isinstance(raw_rows, list) else []
    discovered: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            continue
        if or_id not in available_prices:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        discovered_row: dict[str, Any] = {
            "id": or_id,
            "upstream_id": native_id,
            "display_name": str(row.get("name") or native_id),
            "endpoints": ["chat/completions"],
        }
        context_length = positive_int(row.get("context_length"))
        if context_length is not None:
            discovered_row["context_length"] = context_length
        max_output = positive_int(
            row.get("max_output_length") or row.get("max_output_tokens")
        )
        if max_output is not None:
            discovered_row["max_output_tokens"] = max_output
        discovered[or_id] = discovered_row

    recovered: list[str] = []
    for model_id in sorted(_VERIFIED_PREPAID_MODELS - discovered.keys()):
        price = available_prices.get(model_id)
        native_id = UPSTREAM_ID_MAP.get(model_id)
        if price is None or native_id is None:
            continue
        if not probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=native_id,
            expected_content="PONG",
            max_tokens=256,
        ):
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        discovered[model_id] = _recovered_discovery_row(model_id, native_id)
        recovered.append(model_id)

    prices = {
        model_id: available_prices[model_id]
        for model_id in discovered
        if model_id in available_prices
    }

    _DISCOVERED_MANIFEST_ROWS = discovered

    notes: list[str] = []
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")
    notes.append(
        f"intersected {len(discovered)} account-visible/canary-backed models "
        "with official billing prices"
    )
    if recovered:
        notes.append(
            "recovered catalog omissions after exact PONG canary: " + ", ".join(recovered)
        )

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=PRICE_URL,
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=PRICE_URL,
    )
