"""DigitalOcean Gradient AI serverless catalog and official pricing."""

from __future__ import annotations

import os
import re
from decimal import ROUND_HALF_UP, Decimal
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
from scripts.pricing.model_ids import remember_upstream_id

SLUG = "digitalocean"
URL = "https://inference.do-ai.run/v1/models"
PRICING_URL = (
    "https://docs.digitalocean.com/products/inference/details/pricing/index.html.md"
)
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "digitalocean.json"
)
EXPECTED_MODELS = ["deepseek/deepseek-v4-flash", "z-ai/glm-5.2"]

# Provider id -> (TrustedRouter id, exact label on DigitalOcean's pricing page).
_MODEL_CONFIG = {
    "alibaba-qwen3-32b": ("qwen/qwen3-32b", "Qwen3-32B"),
    "qwen3-coder-flash": ("qwen/qwen3-coder-flash", "Qwen3 Coder Flash"),
    "qwen3.5-397b-a17b": ("qwen/qwen3.5-397b-a17b", "Qwen 3.5 397B A17B"),
    "deepseek-r1-distill-llama-70b": (
        "deepseek/deepseek-r1-distill-llama-70b",
        "DeepSeek R1 Distill Llama 70B",
    ),
    "deepseek-v4-pro": ("deepseek/deepseek-v4-pro", "DeepSeek V4 Pro"),
    "deepseek-4-flash": ("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash"),
    "deepseek-3.2": ("deepseek/deepseek-v3.2", "DeepSeek V3.2"),
    "gemma-4-31B-it": ("google/gemma-4-31b-it", "Gemma 4"),
    "minimax-m2.5": ("minimax/minimax-m2.5", "MiniMax M2.5"),
    "kimi-k2.5": ("moonshotai/kimi-k2.5", "Kimi K2.5"),
    "kimi-k2.6": ("moonshotai/kimi-k2.6", "Kimi K2.6"),
    "llama3.3-70b-instruct": (
        "meta-llama/llama-3.3-70b-instruct",
        "Llama 3.3 Instruct-70B",
    ),
    "llama-4-maverick": (
        "meta-llama/llama-4-maverick",
        "Llama 4 Maverick 17B 128E Instruct",
    ),
    "mistral-3-14B": ("mistralai/ministral-3-14b-instruct", "Ministral 3 14B Instruct"),
    "nemotron-3-ultra-550b": ("nvidia/nemotron-3-ultra-550b", "Nemotron 3 Ultra"),
    "nvidia-nemotron-3-super-120b": (
        "nvidia/nemotron-3-super-120b",
        "Nemotron-3-Super-120B",
    ),
    "nemotron-3-nano-omni": (
        "nvidia/nemotron-3-nano-omni",
        "Nemotron Nano 3 Omni",
    ),
    "nemotron-nano-12b-v2-vl": (
        "nvidia/nemotron-nano-12b-v2-vl",
        "Nemotron Nano 12B v2 VL",
    ),
    "mimo-v2.5": ("xiaomi/mimo-v2.5", "MiMo-V2.5"),
    "mimo-v2.5-pro": ("xiaomi/mimo-v2.5-pro", "MiMo V2.5 Pro"),
    "glm-5.2": ("z-ai/glm-5.2", "GLM-5.2"),
    "glm-5.1": ("z-ai/glm-5.1", "GLM-5.1"),
    "glm-5": ("z-ai/glm-5", "GLM 5"),
}
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, (model_id, _label) in _MODEL_CONFIG.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_PRICE_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?) per 1M tokens")


def _micro_per_m(value: str) -> int:
    return int((Decimal(value) * Decimal("1000000")).to_integral_value(ROUND_HALF_UP))


def _official_prices(markdown: str) -> dict[str, ModelPrice]:
    prices: dict[str, ModelPrice] = {}
    for line in markdown.splitlines():
        if not line.startswith("|") or "Input/output tokens" not in line:
            continue
        for _native_id, (model_id, label) in _MODEL_CONFIG.items():
            if f"[{label}]" not in line:
                continue
            values = _PRICE_RE.findall(line)
            if len(values) < 2:
                continue
            prices[model_id] = ModelPrice(
                prompt_micro_per_m=_micro_per_m(values[0]),
                completion_micro_per_m=_micro_per_m(values[1]),
                prompt_cached_micro_per_m=(
                    _micro_per_m(values[2]) if len(values) >= 3 else None
                ),
            )
            break
    return prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("DIGITAL_OCEAN_API_KEY")
    if not api_key:
        raise RuntimeError("DIGITAL_OCEAN_API_KEY is required for model discovery")
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
        headers={"User-Agent": PROVIDER_FETCH_UA},
    ) as client:
        models_response = client.get(
            URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        models_response.raise_for_status()
        pricing_response = client.get(PRICING_URL, headers={"Accept": "text/markdown"})
        pricing_response.raise_for_status()

    payload = models_response.json()
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        source_rows = []
    live_by_id = {
        row["id"]: row
        for row in source_rows
        if isinstance(row, dict)
        and row.get("owned_by") == "digitalocean"
        and isinstance(row.get("id"), str)
    }
    official = _official_prices(pricing_response.text)
    discovered: dict[str, dict[str, Any]] = {}
    for native_id, (model_id, label) in _MODEL_CONFIG.items():
        source = live_by_id.get(native_id)
        if source is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": label,
        }
        for source_key, output_key in (
            ("context_length", "context_length"),
            ("max_output_tokens", "max_output_tokens"),
        ):
            value = source.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                row[output_key] = value
        discovered[model_id] = row

    _DISCOVERED_MANIFEST_ROWS = discovered
    prices = {model_id: price for model_id, price in official.items() if model_id in discovered}
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=PRICING_URL,
        notes=[
            f"intersected {len(discovered)} live DigitalOcean-owned models with "
            f"{len(prices)} official prices"
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
