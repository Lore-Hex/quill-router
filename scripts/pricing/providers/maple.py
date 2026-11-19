"""Public Maple catalog discovery, separate from encrypted-route qualification.

Never send discovery credentials or a plaintext canary to Maple's logical HTTP
paths. Transport V2 discovery/inference qualification belongs inside the enclave.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html
from scripts.pricing.manifest import write_discovered_chat_manifest
from trusted_router.provider_contracts import MAPLE_MODEL_ID, MAPLE_NATIVE_ID

SLUG = "maple"
PRICING_URL = "https://maple.ai/pricing"
MODEL_DOCS_URL = "https://maple.ai/docs/proxy/models"
# Name the reviewed weights' author, not the inference host. This checkpoint
# must remain distinct from stock DeepSeek and from Maple's other workloads.
MODEL_ID = MAPLE_MODEL_ID
NATIVE_ID = MAPLE_NATIVE_ID
DISPLAY_NAME = "DeepSeek V4.1 Flash Uncensored"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/maple.json"
MANIFEST_STALE_FALLBACK = True
INCLUDE_IN_PRICE_INDEX = True
_CHAT_MODELS = {
    NATIVE_ID: (MODEL_ID, DISPLAY_NAME),
    "glm-5-3-flash": ("z-ai/glm-5.3-flash", "GLM-5.3 Flash"),
    "glm-5-3": ("z-ai/glm-5.3", "GLM 5.3"),
    "kimi-k3-1-pro": ("moonshotai/kimi-k3.1-pro", "Kimi K3.1 Pro"),
    "deepseek-v4-1-flash": ("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
    "gpt-oss-120b": ("openai/gpt-oss-120b", "OpenAI GPT-OSS 120B"),
    "gemma4-31b": ("google/gemma-4-31b-it", "Gemma 4 31B"),
    "llama3-3-70b": ("meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B"),
    "gpt-oss-safeguard-120b": ("openai/gpt-oss-safeguard-120b", "OpenAI GPT-OSS Safeguard 120B API only"),
    # Listed by 2026-11-19. Maple's model library (maple.ai/learn/models) files
    # it under "Thinking Machines Lab's open-weight family", so it names the
    # stock Thinking Machines weights. Held like every row but the qualified
    # workload above.
    "inkling-2-small": ("thinkingmachines/inkling-2-small", "Inkling 2 Small"),
}
UPSTREAM_ID_MAP = {canonical: native for native, (canonical, _) in _CHAT_MODELS.items()}
# Release only after the matching measured gateway image passes every cloud.
_PENDING_ENCLAVE_MODELS: frozenset[str] = frozenset()
_DISCOVERED: dict[str, dict[str, Any]] = {}
# Maple's explicit unit statements; the page moved from the first to the second
# wording on 2026-11-12 without changing the rates.
_USD_PER_M_UNIT_PHRASES = (
    "USD equivalent per 1 million tokens.",
    "USD per 1 million tokens.",
)


def _price(text: str) -> int:
    if re.fullmatch(r"\$[0-9]+(?:\.[0-9]{1,6})?", text) is None:
        raise RuntimeError("maple: expected explicit USD/M token price")
    value = int(Decimal(text[1:]) * 1_000_000)
    if value > 10**12:
        raise RuntimeError("maple: token price out of range")
    return value


def parse_prices(html: str, display_name: str = DISPLAY_NAME) -> ModelPrice:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    if not any(phrase in text for phrase in _USD_PER_M_UNIT_PHRASES):
        raise RuntimeError("maple: token price currency or unit changed")
    # A struck-through "Previously $X" is the superseded rate, not a price
    # in effect; drop it so a cell reads as the single current rate.
    for struck in soup.find_all("s"):
        struck.decompose()
    prices: list[ModelPrice] = []
    required = {"model", "input", "cached input", "output"}
    for table in soup.find_all("table"):
        head = table.find("thead")
        if head is None:
            continue
        headers = [cell.get_text(" ", strip=True).casefold() for cell in head.find_all("th")]
        if not required.issubset(headers):
            continue
        if len(headers) != len(required):
            raise RuntimeError("maple: ambiguous price columns")
        for row in table.find_all("tr"):
            if row.find("td") is None:
                continue
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)]
            if len(cells) != len(headers):
                raise RuntimeError("maple: malformed token-price row")
            values = dict(zip(headers, cells, strict=True))
            if values["model"] != display_name:
                continue
            prompt = _price(values["input"])
            cached_text = values["cached input"]
            cached = (prompt if cached_text in {"\u2013Standard input price applies", "\u2013 Standard input price applies"}
                      else _price(cached_text))
            completion = _price(values["output"])
            if not prompt or not completion or cached > prompt:
                raise RuntimeError("maple: invalid token prices")
            prices.append(ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached))
    if len(prices) != 1:
        raise RuntimeError("maple: release-pinned model price missing or duplicated")
    return prices[0]


def parse_context(html: str, native_id: str = NATIVE_ID) -> int:
    contexts: list[int] = []
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        head = table.find("thead")
        if head is None:
            continue
        headers = [cell.get_text(" ", strip=True).casefold() for cell in head.find_all("th")]
        if not {"model", "context tokens"}.issubset(headers):
            continue
        if len(headers) != len(set(headers)):
            raise RuntimeError("maple: ambiguous capability columns")
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            if len(cells) != len(headers):
                raise RuntimeError("maple: malformed capability row")
            values = dict(zip(headers, cells, strict=True))
            codes = [code.get_text(strip=True) for code in values["model"].find_all("code")]
            if native_id not in codes:
                continue
            text = values["context tokens"].get_text(strip=True)
            if codes != [native_id] or re.fullmatch(r"[1-9][0-9]*(?:,[0-9]{3})*", text) is None:
                raise RuntimeError("maple: ambiguous model or context")
            context = int(text.replace(",", ""))
            if not 1024 <= context <= 1_048_576:
                raise RuntimeError("maple: context requires release review")
            contexts.append(context)
    if len(contexts) != 1:
        raise RuntimeError("maple: release-pinned model capability missing or duplicated")
    return contexts[0]


def parse_catalog(html: str) -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        head = table.find("thead")
        if head is None:
            continue
        headers = [cell.get_text(" ", strip=True).casefold() for cell in head.find_all("th")]
        if not {"model", "context tokens"}.issubset(headers):
            continue
        if len(headers) != len(set(headers)):
            raise RuntimeError("maple: ambiguous capability columns")
        for entry in table.find_all("tr"):
            cells = entry.find_all("td")
            if not cells:
                continue
            if len(cells) != len(headers):
                raise RuntimeError("maple: malformed capability row")
            values = dict(zip(headers, cells, strict=True))
            codes = [code.get_text(strip=True) for code in values["model"].find_all("code")]
            if len(codes) != 1 or codes[0] not in _CHAT_MODELS:
                raise RuntimeError("maple: new or ambiguous model identity requires review")
            native = codes[0]
            canonical, display = _CHAT_MODELS[native]
            if canonical in discovered:
                raise RuntimeError("maple: duplicate model identity")
            row: dict[str, Any] = {
                "id": canonical, "upstream_id": native, "display_name": display,
                "context_length": parse_context(html, native), "model_type": "chat",
                "input_modalities": ["text"], "output_modalities": ["text"],
                "endpoints": ["chat/completions"], "supports_tools": False,
                "supports_reasoning": False, "routable": False,
                "routable_reason": "attested-enclave-qualification-pending",
            }
            # Vendor capabilities are not proof of TR's encrypted adapter or
            # downstream privacy. Existing qualified scope remains text-only.
            if native == NATIVE_ID:
                row["routable"] = canonical not in _PENDING_ENCLAVE_MODELS
                row.pop("routable_reason")
                if not row["routable"]:
                    row["routable_reason"] = "attested-enclave-rollout-pending"
            else:
                flags: dict[str, bool] = {}
                for field in ("thinking", "image input", "tool calls"):
                    value = values[field].get_text(strip=True) if field in values else ""
                    if value not in {"Yes", "No"}:
                        raise RuntimeError("maple: capability requires review")
                    flags[field] = value == "Yes"
                row["supports_tools"] = flags["tool calls"]
                row["supports_reasoning"] = flags["thinking"]
                if flags["image input"]:
                    row["input_modalities"].append("image")
            discovered[canonical] = row
    if MODEL_ID not in discovered:
        raise RuntimeError("maple: release-pinned model capability missing")
    if any(code.get_text(strip=True) == "nomic-embed-text" for code in soup.find_all("code")):
        # Maple documents an alias, not a versioned weights identity or price.
        discovered["maple/nomic-embed-text"] = {
            "id": "maple/nomic-embed-text", "upstream_id": "nomic-embed-text",
            "display_name": "Nomic Embed Text (Maple)", "model_type": "embedding",
            "input_modalities": ["text"], "output_modalities": ["embeddings"],
            "endpoints": ["embeddings"], "routable": False,
            "routable_reason": "price-and-encrypted-embedding-qualification-pending",
        }
    return discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED  # noqa: PLW0603
    _DISCOVERED = {}
    html = fetch_html(PRICING_URL)
    discovered = parse_catalog(fetch_html(MODEL_DOCS_URL))
    prices = {model: parse_prices(html, row["display_name"])
              for model, row in discovered.items() if row["model_type"] == "chat"}
    _DISCOVERED = discovered
    return ProviderPricingResult(
        slug=SLUG, prices=prices, source="html", fetched_url=PRICING_URL,
        notes=["Published token rates only; no inferred credit minimum or rounding fee. "
               "New model discovery cannot clear encrypted transport, privacy or canary holds."],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    expected_prices = {key for key, row in _DISCOVERED.items() if row["model_type"] == "chat"}
    if result.slug != SLUG or set(result.prices) != expected_prices or not _DISCOVERED:
        raise RuntimeError("maple: fresh reviewed price/capability fetch required")
    holds = {key: row["routable_reason"] for key, row in _DISCOVERED.items()
             if row.get("routable") is False}
    if MANIFEST_PATH.exists():
        for row in json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["models"]:
            if row.get("routable") is False and row.get("routable_reason") == "provider-canary-failed":
                holds[row["id"]] = "provider-canary-failed"
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED,
        source_url=MODEL_DOCS_URL, pricing_source_url=PRICING_URL,
        operator_hold_reasons=holds,
    )
