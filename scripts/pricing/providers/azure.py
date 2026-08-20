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
from collections.abc import Mapping
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
    PriceTier,
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
class RetailTierRule:
    max_prompt_tokens: int | None
    required_words: tuple[str, ...] = ()
    excluded_words: tuple[str, ...] = ()
    require_cached: bool = False


@dataclass(frozen=True)
class RetailRule:
    product: str
    stems: tuple[str, ...]
    excluded_stems: tuple[str, ...] = ()
    excluded_words: tuple[str, ...] = ()
    require_cached: bool = False
    production_hold_reason: str | None = None
    price_tiers: tuple[RetailTierRule, ...] = ()


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
    "deepseek/deepseek-v3.2": RetailRule(
        "Azure Deepseek Models",
        ("v3.2",),
        ("v3.2 sp",),
        # Azure caps this deployment at 128,000 tokens; the canonical
        # TrustedRouter route promises 163,840.
        production_hold_reason="context-capability-mismatch",
    ),
    "deepseek/deepseek-v3.2-speciale": RetailRule(
        "Azure Deepseek Models",
        ("v3.2 sp",),
        production_hold_reason="tool-calling-unsupported",
    ),
    "deepseek/deepseek-v4-flash": RetailRule(
        "Azure Deepseek Models",
        ("v4 flash",),
        # DeepSeek-V4-Flash-0731 is a separate native model/checkpoint, not a
        # version of this canonical route.  It needs its own canonical id and
        # retail rule before Azure may publish it.
        excluded_stems=("v4 flash 0731",),
        require_cached=True,
        # Azure documents 1,000,000 tokens, below the canonical 1,048,576.
        production_hold_reason="context-capability-mismatch",
    ),
    "deepseek/deepseek-v4-pro": RetailRule(
        "Azure Deepseek Models",
        ("v4 pro",),
        require_cached=True,
        # Azure documents 1,000,000 tokens, below the canonical 1,048,576.
        production_hold_reason="context-capability-mismatch",
    ),
    "meta-llama/llama-3.3-70b-instruct": RetailRule(
        "Azure Llama Models",
        ("llama 3.3 70b",),
        # Azure caps this deployment at 128,000 tokens; the canonical route
        # promises the binary 131,072-token window.
        production_hold_reason="context-capability-mismatch",
    ),
    "meta-llama/llama-4-maverick": RetailRule(
        "Azure Llama Models",
        ("llama 4 maverick 17b",),
        # Azure documents 1,000,000 tokens, below the canonical 1,048,576.
        production_hold_reason="context-capability-mismatch",
    ),
    "microsoft/phi-4": RetailRule(
        "Azure Phi Models",
        ("phi-4",),
        ("phi-4-mini", "phi-4-reasoning", "phi-4-mini mm"),
        production_hold_reason="tool-calling-unsupported",
    ),
    "microsoft/phi-4-mini-instruct": RetailRule(
        "Azure Phi Models",
        ("phi-4-mini",),
        ("phi-4-mini-reasoning", "phi-4-mini mm"),
        production_hold_reason="tool-calling-unsupported",
    ),
    "microsoft/phi-4-mini-reasoning": RetailRule(
        "Azure Phi Models",
        ("phi-4-mini-reasoning",),
        production_hold_reason="tool-calling-unsupported",
    ),
    "microsoft/phi-4-multimodal-instruct": RetailRule(
        "Azure Phi Models",
        ("phi-4-mini mm",),
        production_hold_reason="launch-capability-unverified",
    ),
    "microsoft/phi-4-reasoning": RetailRule(
        "Azure Phi Models",
        ("phi-4-reasoning",),
        excluded_words=("plus",),
        production_hold_reason="tool-calling-unsupported",
    ),
    "mistralai/codestral-2501": RetailRule(
        "Azure Mistral Models",
        ("codestral",),
        production_hold_reason="tool-calling-unsupported",
    ),
    "mistralai/mistral-large-3": RetailRule("Azure Mistral Models", ("large 3",)),
    "moonshotai/kimi-k2.5": RetailRule(
        "Azure Kimi",
        ("k2.5",),
        require_cached=True,
    ),
    "moonshotai/kimi-k2.6": RetailRule(
        "Azure Kimi",
        ("k2.6",),
        require_cached=True,
    ),
    "moonshotai/kimi-k2.7-code": RetailRule(
        "Azure Kimi",
        ("k2.7 code",),
        require_cached=True,
    ),
    "openai/gpt-5-mini": RetailRule(
        "Azure OpenAI GPT5",
        ("gpt 5 mini", "5 mini"),
        require_cached=True,
    ),
    "openai/gpt-5.4-mini": RetailRule(
        "Azure OpenAI GPT5",
        ("5.4 mini",),
        require_cached=True,
        production_hold_reason="global-sku-unavailable",
    ),
    "openai/gpt-oss-120b": RetailRule("Azure OpenAI OSS Models", ("gpt-oss-120b",)),
    "x-ai/grok-4.1-fast-non-reasoning": RetailRule(
        "Azure Grok Models",
        ("grok 4.1",),
    ),
    "x-ai/grok-4.1-fast-reasoning": RetailRule(
        "Azure Grok Models",
        ("grok 4.1",),
    ),
    "x-ai/grok-4.20-non-reasoning": RetailRule("Azure Grok Models", ("grok 4.2",)),
    "x-ai/grok-4.20-reasoning": RetailRule("Azure Grok Models", ("grok 4.2",)),
    "x-ai/grok-4.3": RetailRule(
        "Azure Grok Models",
        ("4.3",),
        # Microsoft documents this Azure deployment at 200k context while the
        # TrustedRouter canonical model advertises 1M.  Keep it dark until an
        # authenticated >200k canary and billed-meter evidence prove that the
        # endpoint can safely serve the canonical capability.
        production_hold_reason="context-capability-unverified",
        price_tiers=(
            RetailTierRule(
                200_000,
                excluded_words=("l",),
                require_cached=True,
            ),
            RetailTierRule(
                None,
                required_words=("l",),
                require_cached=True,
            ),
        ),
    ),
}

# A retail meter names a model family, not an immutable checkpoint.  Bind each
# family to the exact Foundry version whose price/deployment pairing has been
# verified.  A newly advertised default must remain dark until this contract
# is reviewed, updated, deployed with NoAutoUpgrade, and canaried.
_ALLOWED_MODEL_VERSIONS: dict[str, tuple[str, ...]] = {
    "cohere/command-a": ("1",),
    "cohere/command-a-plus-05-2026": ("1",),
    "deepseek/deepseek-v3.2": ("1",),
    "deepseek/deepseek-v3.2-speciale": ("1",),
    "deepseek/deepseek-v4-flash": ("2026-04-23",),
    "deepseek/deepseek-v4-pro": ("2026-04-23",),
    "meta-llama/llama-3.3-70b-instruct": ("9",),
    "meta-llama/llama-4-maverick": ("1",),
    "microsoft/phi-4": ("7",),
    "microsoft/phi-4-mini-instruct": ("1",),
    "microsoft/phi-4-mini-reasoning": ("1",),
    "microsoft/phi-4-multimodal-instruct": ("1",),
    "microsoft/phi-4-reasoning": ("1",),
    "mistralai/codestral-2501": ("2",),
    "mistralai/mistral-large-3": ("1",),
    "moonshotai/kimi-k2.5": ("1",),
    "moonshotai/kimi-k2.6": ("2026-04-20",),
    "moonshotai/kimi-k2.7-code": ("2026-06-12",),
    "openai/gpt-5-mini": ("2025-08-07",),
    "openai/gpt-5.4-mini": ("2026-03-17",),
    "openai/gpt-oss-120b": ("1",),
    "x-ai/grok-4.1-fast-non-reasoning": ("1",),
    "x-ai/grok-4.1-fast-reasoning": ("1",),
    "x-ai/grok-4.20-non-reasoning": ("1",),
    "x-ai/grok-4.20-reasoning": ("1",),
    "x-ai/grok-4.3": ("1",),
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
    model_id: str,
    model_version: str | None,
    kind: str,
    tier_rule: RetailTierRule,
) -> int | None:
    candidates: set[tuple[str, str, int]] = set()
    required_words = frozenset(tier_rule.required_words)
    excluded_words = frozenset((*rule.excluded_words, *tier_rule.excluded_words))
    for row in rows:
        if row.get("productName") != rule.product:
            continue
        text = f"{row.get('skuName', '')} {row.get('meterName', '')}"
        words = _words(text)
        if (
            not _matches_stem(text, rule.stems)
            or _matches_stem(text, rule.excluded_stems)
            or not required_words.issubset(words)
            or words & excluded_words
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
        candidates.add(
            (
                str(row.get("skuName") or "").strip().lower(),
                str(row.get("meterName") or "").strip().lower(),
                _micro_per_million(row),
            )
        )
    if len(candidates) > 1:
        version = model_version or "<unspecified>"
        raise ValueError(
            f"ambiguous Azure {kind} price for {model_id} version {version} "
            f"({rule.product}/{rule.stems}): {sorted(candidates)}"
        )
    candidate = next(iter(candidates), None)
    return candidate[2] if candidate is not None else None


def retail_model_ids() -> frozenset[str]:
    """Return models whose prices can be selected from Microsoft's feed."""

    rule_ids = frozenset(_RETAIL_RULES)
    versioned_ids = frozenset(_ALLOWED_MODEL_VERSIONS)
    if rule_ids != versioned_ids:
        raise RuntimeError(
            "Azure retail rules and exact model-version contracts differ: "
            f"rules_only={sorted(rule_ids - versioned_ids)}, "
            f"versions_only={sorted(versioned_ids - rule_ids)}"
        )
    return frozenset(
        model_id
        for model_id, rule in _RETAIL_RULES.items()
        if rule.production_hold_reason is None
    )


def retail_model_versions() -> dict[str, frozenset[str]]:
    """Return a copy of the exact deployable checkpoint contract."""

    return {
        model_id: frozenset(_ALLOWED_MODEL_VERSIONS[model_id])
        for model_id in retail_model_ids()
    }


def _manifest_model_versions() -> dict[str, str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("azure manifest has no models list")

    versions: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or model_id not in _RETAIL_RULES:
            continue
        version = row.get("azure_model_version")
        if not isinstance(version, str) or not version.strip():
            raise RuntimeError(f"azure manifest model {model_id} has no azure_model_version")
        previous = versions.setdefault(model_id, version)
        if previous != version:
            raise RuntimeError(
                f"azure manifest model {model_id} has conflicting versions: "
                f"{previous!r} and {version!r}"
            )
    return versions


def parse_retail_prices(
    rows: list[dict[str, Any]],
    *,
    model_versions: Mapping[str, str] | None = None,
) -> dict[str, ModelPrice]:
    # Do not copy prices from a provider's direct API. A model is eligible for
    # Azure only when Microsoft's Retail Prices API exposes an unambiguous
    # meter for the exact Azure SKU. In particular, Azure currently publishes
    # no Claude/Anthropic meters, so those deployments remain dark even if
    # Marketplace terms become accepted later.
    prices: dict[str, ModelPrice] = {}
    for model_id, rule in _RETAIL_RULES.items():
        allowed_versions = _ALLOWED_MODEL_VERSIONS.get(model_id)
        if allowed_versions is None:
            raise RuntimeError(f"Azure retail rule {model_id} has no version contract")
        model_version: str | None = None
        # Production callers always supply the deployed/selected model
        # versions.  Skip models absent from this account before touching their
        # meters, so an unavailable model's ambiguous feed rows cannot abort a
        # healthy sync.  A present but unreviewed checkpoint remains dark.
        if model_versions is not None:
            if rule.production_hold_reason is not None:
                continue
            if model_id not in model_versions:
                continue
            model_version = model_versions[model_id]
            if model_version not in allowed_versions:
                continue
        tier_rules = rule.price_tiers or (RetailTierRule(None),)
        tiers: list[PriceTier] = []
        for tier_rule in tier_rules:
            prompt = _retail_rate(
                rows,
                rule,
                model_id=model_id,
                model_version=model_version,
                kind="input",
                tier_rule=tier_rule,
            )
            completion = _retail_rate(
                rows,
                rule,
                model_id=model_id,
                model_version=model_version,
                kind="output",
                tier_rule=tier_rule,
            )
            cached = _retail_rate(
                rows,
                rule,
                model_id=model_id,
                model_version=model_version,
                kind="cached",
                tier_rule=tier_rule,
            )
            if (
                prompt is None
                or completion is None
                or ((rule.require_cached or tier_rule.require_cached) and cached is None)
            ):
                tiers = []
                break
            tiers.append(
                PriceTier(
                    tier_rule.max_prompt_tokens,
                    prompt,
                    completion,
                    cached,
                )
            )
        if tiers:
            prices[model_id] = ModelPrice(tiers=tiers)
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
        prices=parse_retail_prices(
            fetch_retail_rows(),
            model_versions=_manifest_model_versions(),
        ),
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
