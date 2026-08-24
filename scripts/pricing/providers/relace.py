"""Relace first-party open-model pricing table and paid route canaries."""

from __future__ import annotations

import os
import re
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
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import remember_upstream_id
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "relace"
BASE_URL = "https://models.relace.ai/v1"
URL = "https://docs.relace.ai/docs/open-models/quickstart.md"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "relace.json"
)
EXPLICIT_MODEL_MAP = {
    "deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek/deepseek-v4-flash-0731",
    "moonshotai/kimi-k3": "moonshotai/kimi-k3",
}
EXPECTED_MODELS = list(EXPLICIT_MODEL_MAP.values())
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
MANIFEST_STALE_FALLBACK = True

_TABLE_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|"
    r"\s*\\?\$([0-9.]+)\s*/\s*M\s*\|\s*\\?\$([0-9.]+)\s*/\s*M\s*\|"
    r"\s*\\?\$([0-9.]+)\s*/\s*M\s*\|\s*$"
)


def _micro_per_m(raw: str) -> int:
    return int(Decimal(raw) * Decimal(1_000_000))


def _context_tokens(raw: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KkMm])\s*", raw)
    if match is None:
        raise RuntimeError(f"relace: unsupported context value {raw!r}")
    multiplier = 1_000 if match.group(2).casefold() == "k" else 1_000_000
    return int(Decimal(match.group(1)) * multiplier)


def _parse_quickstart(
    markdown: str,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for line in markdown.splitlines():
        match = _TABLE_ROW.match(line)
        if match is None:
            continue
        display_name, native_id, context, prompt, completion, cached = match.groups()
        model_id = EXPLICIT_MODEL_MAP.get(native_id)
        if model_id is None:
            # The first-party table is allowed to grow only after a human has
            # selected a stable public TrustedRouter ID for the new route.
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=_micro_per_m(prompt),
            completion_micro_per_m=_micro_per_m(completion),
            prompt_cached_micro_per_m=_micro_per_m(cached),
        )
        rows[model_id] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": display_name.strip(),
            "model_type": "chat",
            "context_length": _context_tokens(context),
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "supported_features": [
                "chat",
                "completion",
                "tools",
                "json_mode",
                "structured_outputs",
                "prompt_caching",
            ],
            "status": 1,
        }
    return prices, rows


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("RELACE_API_KEY")
    if not api_key:
        raise RuntimeError("relace: RELACE_API_KEY is required for route canaries")
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers={"User-Agent": PROVIDER_FETCH_UA})
        response.raise_for_status()
        markdown = response.text

    prices, discovered = _parse_quickstart(markdown)
    checked = models_requiring_canary(MANIFEST_PATH, discovered)
    healthy = {
        model_id
        for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=UPSTREAM_ID_MAP[model_id],
            expected_content="PONG",
            max_tokens=256,
        )
    }
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
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
        notes=[
            f"parsed {len(discovered)} Relace hosted open models",
            f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
        pricing_source_url=URL,
    )
