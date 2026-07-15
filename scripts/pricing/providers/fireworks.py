"""Fireworks AI — human-only provider config.

Fireworks publishes a first-party serverless pricing table for its headline
models. We fetch that docs page and parse the standard serving-path prices.
Prices become routable only when the authenticated operator model list also
contains the model. The supplemental manifest is pruned from that intersection.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    fetch_json,
    fetch_provider,
    validate,
)

SLUG = "fireworks"
URL = "https://r.jina.ai/https://docs.fireworks.ai/serverless/pricing"
MODELS_URL = "https://api.fireworks.ai/inference/v1/models"
JINA_HEADERS = {"X-Return-Format": "markdown"}
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "fireworks.json"
)

EXPECTED_MODELS = [
    "moonshotai/kimi-k2.6",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "z-ai/glm-5.1",
    "openai/gpt-oss-120b",
]

_NATIVE_TO_CANONICAL = {
    "accounts/fireworks/models/kimi-k2p6": "moonshotai/kimi-k2.6",
    "accounts/fireworks/models/kimi-k2p7-code": "moonshotai/kimi-k2.7-code",
    "accounts/fireworks/models/deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "accounts/fireworks/models/deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "accounts/fireworks/models/glm-5p2": "z-ai/glm-5.2",
    "accounts/fireworks/models/glm-5p1": "z-ai/glm-5.1",
    "accounts/fireworks/models/gpt-oss-120b": "openai/gpt-oss-120b",
    "accounts/fireworks/models/gpt-oss-20b": "openai/gpt-oss-20b",
    "accounts/fireworks/models/minimax-m3": "minimax/minimax-m3",
    "accounts/fireworks/models/minimax-m2p7": "minimax/minimax-m2.7",
}
UPSTREAM_ID_MAP = {canonical: native for native, canonical in _NATIVE_TO_CANONICAL.items()}
# Fast is an account router rather than a row in /v1/models. It remains an
# explicit, separately smoke-tested route and is not subject to model pruning.
UPSTREAM_ID_MAP["z-ai/glm-5.2-fast"] = "accounts/fireworks/routers/glm-5p2-fast"
_DISCOVERED_LIVE_MODEL_IDS: set[str] = set()


def _live_model_ids() -> set[str]:
    api_key = os.environ.get("FIREWORKS_API_KEY") or os.environ.get("FIREWORKS_AI_API_KEY")
    if not api_key:
        raise RuntimeError("fireworks: FIREWORKS_API_KEY is required")
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("fireworks: /v1/models response has no data list")
    discovered: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        canonical = _NATIVE_TO_CANONICAL.get(native_id)
        if canonical is not None:
            discovered.add(canonical)
    return discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_LIVE_MODEL_IDS

    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
    live_model_ids = _live_model_ids()
    _DISCOVERED_LIVE_MODEL_IDS = live_model_ids
    docs_only = sorted(set(result.prices) - live_model_ids)
    result.prices = {
        model_id: price for model_id, price in result.prices.items() if model_id in live_model_ids
    }
    errors = validate(result.prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    if docs_only:
        result.notes.append(
            "official pricing rows not enabled for this Fireworks account: " + ", ".join(docs_only)
        )
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Refresh prices and remove retired Fireworks model routes.

    Account routers are preserved because Fireworks does not expose them in
    the model-list API. Ordinary model routes must be present in the
    authenticated operator catalog and on the official pricing page.
    """

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("fireworks manifest has no models list")

    retained: list[dict[str, Any]] = []
    updated = 0
    removed: list[str] = []
    for candidate in rows:
        if not isinstance(candidate, dict):
            continue
        model_id = candidate.get("id")
        upstream_id = candidate.get("upstream_id")
        if not isinstance(model_id, str) or not isinstance(upstream_id, str):
            continue
        is_model_route = upstream_id.startswith("accounts/fireworks/models/")
        if is_model_route and model_id not in _DISCOVERED_LIVE_MODEL_IDS:
            removed.append(model_id)
            continue

        price = result.prices.get(model_id)
        if price is not None:
            tier = price.tiers[0]
            candidate["input_token_price_per_m"] = tier.prompt_micro_per_m
            candidate["output_token_price_per_m"] = tier.completion_micro_per_m
            if tier.prompt_cached_micro_per_m is not None:
                candidate["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
            else:
                candidate.pop("cached_input_token_price_per_m", None)
            updated += 1
        retained.append(candidate)

    if not retained:
        raise RuntimeError("fireworks manifest pruning removed every route")
    rows[:] = retained
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    suffix = f", removed {len(removed)} unavailable" if removed else ""
    return [f"fireworks: refreshed provider_models/fireworks.json ({updated} priced rows{suffix})"]
