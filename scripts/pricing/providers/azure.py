"""Microsoft Foundry model pricing and canonical model identity.

Azure exposes the account-scoped model/quota catalog through ARM and exact
regional list prices through the public Retail Prices API.  This adapter keeps
those two concerns separate: the deployment sync owns availability, while this
module resolves prices without guessing.  A model with an ambiguous or missing
price is deliberately excluded by the sync.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
from scripts.pricing.manifest import write_discovered_chat_manifest

SLUG = "azure"
URL = (
    "https://prices.azure.com/api/retail/prices"
    "?$filter=serviceName%20eq%20%27Foundry%20Models%27"
    "%20and%20armRegionName%20eq%20%27eastus2%27"
)
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "azure.json"
)


@dataclass(frozen=True)
class RetailRule:
    product: str
    stems: tuple[str, ...]
    excluded_stems: tuple[str, ...] = ()
    excluded_words: tuple[str, ...] = ()


_CANONICAL_IDS: dict[str, str] = {
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-opus-4-1": "anthropic/claude-opus-4.1",
    "claude-opus-4-5": "anthropic/claude-opus-4.5",
    "claude-opus-4-6": "anthropic/claude-opus-4.6",
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4.5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "cohere-command-a": "cohere/command-a",
    "cohere-command-a-plus-05-2026": "cohere/command-a-plus-05-2026",
    "codestral-2501": "mistralai/codestral-2501",
    "deepseek-v3.2": "deepseek/deepseek-v3.2",
    "deepseek-v3.2-speciale": "deepseek/deepseek-v3.2-speciale",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "gpt-5-mini": "openai/gpt-5-mini",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "grok-4-1-fast-non-reasoning": "x-ai/grok-4.1-fast-non-reasoning",
    "grok-4-1-fast-reasoning": "x-ai/grok-4.1-fast-reasoning",
    "grok-4-20-non-reasoning": "x-ai/grok-4.20-non-reasoning",
    "grok-4-20-reasoning": "x-ai/grok-4.20-reasoning",
    "grok-4.3": "x-ai/grok-4.3",
    "kimi-k2.5": "moonshotai/kimi-k2.5",
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "kimi-k2.7-code": "moonshotai/kimi-k2.7-code",
    "llama-3.3-70b-instruct": "meta-llama/llama-3.3-70b-instruct",
    "llama-4-maverick-17b-128e-instruct-fp8": "meta-llama/llama-4-maverick",
    "mistral-large-3": "mistralai/mistral-large-3",
    "phi-4": "microsoft/phi-4",
    "phi-4-mini-instruct": "microsoft/phi-4-mini-instruct",
    "phi-4-mini-reasoning": "microsoft/phi-4-mini-reasoning",
    "phi-4-multimodal-instruct": "microsoft/phi-4-multimodal-instruct",
    "phi-4-reasoning": "microsoft/phi-4-reasoning",
}

_RETAIL_RULES: dict[str, RetailRule] = {
    "cohere/command-a": RetailRule("Cohere Models", ("command a",), ("command a plus",)),
    "cohere/command-a-plus-05-2026": RetailRule("Cohere Models", ("command a plus",)),
    "deepseek/deepseek-v3.2": RetailRule("Azure Deepseek Models", ("v3.2",), ("v3.2 sp",)),
    "deepseek/deepseek-v3.2-speciale": RetailRule("Azure Deepseek Models", ("v3.2 sp",)),
    "deepseek/deepseek-v4-flash": RetailRule("Azure Deepseek Models", ("v4 flash",)),
    "deepseek/deepseek-v4-pro": RetailRule("Azure Deepseek Models", ("v4 pro",)),
    "meta-llama/llama-3.3-70b-instruct": RetailRule("Azure Llama Models", ("llama 3.3 70b",)),
    "meta-llama/llama-4-maverick": RetailRule("Azure Llama Models", ("llama 4 maverick 17b",)),
    "microsoft/phi-4": RetailRule(
        "Azure Phi Models",
        ("phi-4",),
        ("phi-4-mini", "phi-4-reasoning", "phi-4-mini mm"),
    ),
    "microsoft/phi-4-mini-instruct": RetailRule(
        "Azure Phi Models",
        ("phi-4-mini",),
        ("phi-4-mini-reasoning", "phi-4-mini mm"),
    ),
    "microsoft/phi-4-mini-reasoning": RetailRule("Azure Phi Models", ("phi-4-mini-reasoning",)),
    "microsoft/phi-4-multimodal-instruct": RetailRule("Azure Phi Models", ("phi-4-mini mm",)),
    "microsoft/phi-4-reasoning": RetailRule("Azure Phi Models", ("phi-4-reasoning",)),
    "mistralai/codestral-2501": RetailRule("Azure Mistral Models", ("codestral",)),
    "mistralai/mistral-large-3": RetailRule("Azure Mistral Models", ("large 3",)),
    "moonshotai/kimi-k2.5": RetailRule("Azure Kimi", ("k2.5",)),
    "moonshotai/kimi-k2.6": RetailRule("Azure Kimi", ("k2.6",)),
    "moonshotai/kimi-k2.7-code": RetailRule("Azure Kimi", ("k2.7 code",)),
    "openai/gpt-5-mini": RetailRule("Azure OpenAI GPT5", ("gpt 5 mini", "5 mini")),
    "openai/gpt-5.4-mini": RetailRule("Azure OpenAI GPT5", ("5.4 mini",)),
    "openai/gpt-oss-120b": RetailRule("Azure OpenAI OSS Models", ("gpt-oss-120b",)),
    "x-ai/grok-4.1-fast-non-reasoning": RetailRule("Azure Grok Models", ("grok 4.1", "grok4 fast")),
    "x-ai/grok-4.1-fast-reasoning": RetailRule("Azure Grok Models", ("grok 4.1", "grok4 fast")),
    "x-ai/grok-4.20-non-reasoning": RetailRule("Azure Grok Models", ("grok 4.2",)),
    "x-ai/grok-4.20-reasoning": RetailRule("Azure Grok Models", ("grok 4.2",)),
    "x-ai/grok-4.3": RetailRule("Azure Grok Models", ("4.3",), excluded_words=("l",)),
}

_INPUT_WORDS = frozenset({"inp", "inpt", "input"})
_OUTPUT_WORDS = frozenset({"outp", "outpt", "output", "opt"})
_CACHE_WORDS = frozenset({"cached", "cchd", "cd"})
_EXCLUDED_WORDS = frozenset({"batch", "dz", "dzone", "regional", "regnl", "ft", "finetuned", "pp"})


def canonical_model_id(native_name: str) -> str | None:
    return _CANONICAL_IDS.get(native_name.strip().lower())


def deployment_name(native_name: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", native_name.strip().lower()).strip("-")
    return value[:64]


def _words(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9.]+", value.lower()))


def _micro_per_million(row: dict[str, Any]) -> int:
    price = Decimal(str(row["retailPrice"]))
    unit = str(row.get("unitOfMeasure") or "")
    if unit == "1K":
        dollars_per_million = price * Decimal(1000)
    elif unit == "1M":
        dollars_per_million = price
    else:
        raise ValueError(f"unsupported Azure token price unit: {unit}")
    return int(dollars_per_million * Decimal(1_000_000))


def _matches_stem(text: str, stems: tuple[str, ...]) -> bool:
    normalized = " ".join(re.findall(r"[a-z0-9.]+", text.lower()))
    normalized_stems = (" ".join(re.findall(r"[a-z0-9.]+", stem.lower())) for stem in stems)
    return any(stem in normalized for stem in normalized_stems)


def _retail_rate(
    rows: list[dict[str, Any]],
    rule: RetailRule,
    *,
    kind: str,
) -> int | None:
    candidates: set[int] = set()
    for row in rows:
        if row.get("productName") != rule.product:
            continue
        text = f"{row.get('skuName', '')} {row.get('meterName', '')}"
        words = _words(text)
        if (
            not _matches_stem(text, rule.stems)
            or _matches_stem(text, rule.excluded_stems)
            or words & frozenset(rule.excluded_words)
            or words & _EXCLUDED_WORDS
        ):
            continue
        is_cached = bool(words & _CACHE_WORDS)
        if kind == "cached":
            if not is_cached:
                continue
        elif kind == "input":
            if is_cached or not words & _INPUT_WORDS:
                continue
        elif kind == "output":
            if is_cached or not words & _OUTPUT_WORDS:
                continue
        else:
            raise ValueError(f"unknown Azure price kind: {kind}")
        candidates.add(_micro_per_million(row))
    if len(candidates) > 1:
        raise ValueError(
            f"ambiguous Azure {kind} price for {rule.product}/{rule.stems}: {sorted(candidates)}"
        )
    return next(iter(candidates), None)


def parse_retail_prices(rows: list[dict[str, Any]]) -> dict[str, ModelPrice]:
    # Do not copy prices from a provider's direct API. A model is eligible for
    # Azure only when Microsoft's Retail Prices API exposes an unambiguous
    # meter for the exact Azure SKU. In particular, Azure currently publishes
    # no Claude/Anthropic meters, so those deployments remain dark even if
    # Marketplace terms become accepted later.
    prices: dict[str, ModelPrice] = {}
    for model_id, rule in _RETAIL_RULES.items():
        prompt = _retail_rate(rows, rule, kind="input")
        completion = _retail_rate(rows, rule, kind="output")
        if prompt is None or completion is None:
            continue
        cached = _retail_rate(rows, rule, kind="cached")
        prices[model_id] = ModelPrice(
            prompt,
            completion,
            prompt_cached_micro_per_m=cached,
        )
    return prices


def fetch_retail_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_url: str | None = URL
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
        headers={"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"},
    ) as client:
        while next_url:
            response = client.get(next_url)
            response.raise_for_status()
            payload = response.json()
            page = payload.get("Items") if isinstance(payload, dict) else None
            if not isinstance(page, list):
                raise ValueError("Azure Retail Prices response has no Items list")
            rows.extend(row for row in page if isinstance(row, dict))
            raw_next = payload.get("NextPageLink")
            next_url = raw_next if isinstance(raw_next, str) and raw_next else None
    return rows


def fetch() -> ProviderPricingResult:
    result = ProviderPricingResult(
        slug=SLUG,
        prices=parse_retail_prices(fetch_retail_rows()),
        source="api",
        fetched_url=URL,
    )
    errors = validate(result.prices, [])
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("azure manifest has no models list")
    discovered = {
        row["id"]: dict(row)
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=discovered,
        source_url=URL,
    )
