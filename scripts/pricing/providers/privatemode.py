"""Authenticated discovery intersected with release-pinned encrypted workloads.

Discovery is metadata-only. Never canary against the public API with a plaintext
prompt: live inference probes must use the attested enclave adapter.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    ModelPrice,
    ProviderPricingResult,
    fetch_html,
)
from scripts.pricing.currency import ECB_FX_URL, eur_microdollars_per_million, usd_per_eur
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.transcription import parse_price as parse_audio_price

SLUG = "privatemode"
CATALOG_URL = "https://api.privatemode.ai/v1/models"
PRICING_URL = "https://docs.privatemode.ai/pricing/"
MODEL_DOCS_URL = "https://docs.privatemode.ai/models/overview/"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/privatemode.json"
MANIFEST_STALE_FALLBACK = True
INCLUDE_IN_PRICE_INDEX = True

# Mirrors the enclave's pinned manifest and model allowlist. New workloads
# require review, even when they appear in discovery or the pricing page.
MODELS = {
    "glm-5.3": ("z-ai/glm-5.3", "GLM-5.3", 1_000_000, False),
    "glm-5.3-flash": ("z-ai/glm-5.3-flash", "GLM-5.3-Flash", 1_000_000, True),
    "gpt-oss-120b": ("openai/gpt-oss-120b", "gpt-oss-120b", 128_000, False),
    "deepseek-ocr-2": ("deepseek/deepseek-ocr-2", "DeepSeek OCR 2", 8192, True),
    "qwen3-embedding-4b": ("qwen/qwen3-embedding-4b", "Qwen3-Embedding 4B", 32_768, False),
    "voxtral-mini-3b": ("mistralai/voxtral-mini-3b-2507", "Voxtral Mini 3B", 0, False),
    "whisper-large-v3": ("openai/whisper-large-v3", "Whisper large-v3", 0, False),
}
_EMBEDDING_IDS = frozenset({"qwen/qwen3-embedding-4b"})
_TRANSCRIPTION_MODELS = frozenset({"voxtral-mini-3b", "whisper-large-v3"})
# Cleared only after a verified encrypted canary on the matching enclave release.
_PENDING_ENCLAVE_MODELS: frozenset[str] = frozenset()
UPSTREAM_ID_MAP = {row[0]: native for native, row in MODELS.items()}
_DISCOVERED: dict[str, dict[str, Any]] = {}


def canonical_model_id(native_id: str) -> str | None:
    row = MODELS.get(native_id)
    return row[0] if row else None


def _price(value: str, rate: Decimal) -> int:
    if re.fullmatch(r"(?:EUR|€)\s+[0-9]+(?:\.[0-9]+)?", value) is None:
        raise RuntimeError("privatemode: expected an explicit EUR/M token price")
    return eur_microdollars_per_million(Decimal(value.split()[-1]), rate)


def parse_prices(html: str, rate: Decimal) -> dict[str, ModelPrice]:
    prices: dict[str, ModelPrice] = {}
    names = {row[1].casefold(): row[0] for row in MODELS.values()}
    required = {"model", "input", "output", "cached input"}
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        headers = [c.get_text(" ", strip=True).casefold() for c in table.find_all("th")]
        embedding_table = set(headers) == {"model", "input"} and len(headers) == 2
        if not embedding_table and (set(headers) != required or len(headers) != len(required)):
            continue
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if not cells:
                continue
            if len(cells) != len(headers):
                raise RuntimeError("privatemode: malformed token-price row")
            row = dict(zip(headers, cells, strict=True))
            model_id = names.get(row["model"].casefold())
            if model_id is None:
                continue
            if embedding_table:
                if model_id not in _EMBEDDING_IDS:
                    continue
                if model_id in prices:
                    raise RuntimeError("privatemode: duplicate token-price row")
                prompt = _price(row["input"], rate)
                if prompt <= 0:
                    raise RuntimeError("privatemode: invalid embedding price")
                prices[model_id] = ModelPrice(prompt, 0)
                continue
            if model_id in _EMBEDDING_IDS:
                raise RuntimeError("privatemode: embedding listed with output token prices")
            cached = _price(row["cached input"], rate)
            price = ModelPrice(_price(row["input"], rate), _price(row["output"], rate),
                               prompt_cached_micro_per_m=cached)
            if price.prompt_micro_per_m <= 0 or price.completion_micro_per_m <= 0 or (
                not 0 < cached <= price.prompt_micro_per_m
            ):
                raise RuntimeError("privatemode: invalid token prices")
            if model_id in prices:
                raise RuntimeError("privatemode: duplicate token-price row")
            prices[model_id] = price
    if not prices:
        raise RuntimeError("privatemode: token-price table missing")
    return prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED  # noqa: PLW0603
    _DISCOVERED = {}
    key = os.environ.get("PRIVATEMODE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("privatemode: PRIVATEMODE_API_KEY is required")
    with httpx.Client(timeout=PROVIDER_FETCH_TIMEOUT, follow_redirects=False) as client:
        response = client.get(CATALOG_URL, headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("privatemode: model catalog is empty or malformed")
    native_ids = {row.get("id") for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    rate = usd_per_eur(fetch_html(ECB_FX_URL))
    pricing_html = fetch_html(PRICING_URL)
    published = parse_prices(pricing_html, rate)
    prices: dict[str, ModelPrice] = {}
    for native, (model_id, name, context, vision) in MODELS.items():
        available = native in native_ids and model_id in published
        embedding = model_id in _EMBEDDING_IDS
        reasoning_chat = native in {"glm-5.3", "glm-5.3-flash", "gpt-oss-120b"}
        row: dict[str, Any] = {
            "id": model_id, "upstream_id": native, "display_name": name,
            "context_length": context, "input_modalities": ["text", "image"] if vision else ["text"],
            "model_type": "embedding" if embedding else "chat",
            "endpoints": ["embeddings"] if embedding else ["chat/completions"],
            "output_modalities": ["embeddings"] if embedding else ["text"],
            "supports_tools": reasoning_chat, "supports_reasoning": reasoning_chat,
            "routable": available,
        }
        if native in _TRANSCRIPTION_MODELS:
            minute_price = parse_audio_price(model_id, pricing_html, usd_eur_rate=rate) if native in native_ids else 0
            row.update({
                "model_type": "audio", "input_modalities": ["audio"],
                "output_modalities": ["transcription"],
                "endpoints": ["audio/transcriptions"], "routable": native in native_ids,
                "billing_unit": "audio_minute", "audio_minute_price_microdollars": minute_price,
            })
            if native not in native_ids:
                row["routable_reason"] = "delisted-upstream"
            row.pop("context_length")
            _DISCOVERED[model_id] = row
            continue
        if not available:
            row["routable_reason"] = "delisted-upstream" if native not in native_ids else "price-unavailable"
        if available:
            prices[model_id] = published[model_id]
            if native in _PENDING_ENCLAVE_MODELS:
                row["routable"] = False
                row["routable_reason"] = "attested-enclave-rollout-pending"
        _DISCOVERED[model_id] = row
    if not prices:
        raise RuntimeError("privatemode: no priced release-pinned models")
    return ProviderPricingResult(slug=SLUG, prices=prices, source="api", fetched_url=CATALOG_URL,
                                 notes=[f"EUR converted at ECB USD/EUR {rate} plus 5% FX reserve; metadata-only discovery"])


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    holds = {key: row["routable_reason"] for key, row in _DISCOVERED.items()
             if row.get("routable") is False}
    if MANIFEST_PATH.exists():
        # Listing/pricing cannot prove recovery from failed encrypted inference.
        # An operator must release this hold after a successful enclave canary.
        for row in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["models"]:
            if row.get("routable") is False and row.get("routable_reason") == "provider-canary-failed":
                holds[row["id"]] = "provider-canary-failed"
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED,
        source_url=MODEL_DOCS_URL, pricing_source_url=PRICING_URL,
        operator_hold_reasons=holds,
    )
