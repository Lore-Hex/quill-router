"""Public prices for the release-pinned encrypted Maple workload.

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

SLUG = "maple"
PRICING_URL = "https://maple.ai/pricing"
MODEL_DOCS_URL = "https://maple.ai/docs/proxy/models"
MODEL_ID = "maple/deepseek-v4-1-flash-1017-uncensored"
NATIVE_ID = "deepseek-v4-1-flash-1017-uncensored"
DISPLAY_NAME = "DeepSeek V4.1 Flash Uncensored"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/maple.json"
MANIFEST_STALE_FALLBACK = True
INCLUDE_IN_PRICE_INDEX = True
UPSTREAM_ID_MAP = {MODEL_ID: NATIVE_ID}
# Release only after the matching measured gateway image passes every cloud.
_PENDING_ENCLAVE_MODELS = frozenset({MODEL_ID})
_DISCOVERED: dict[str, dict[str, Any]] = {}


def _price(text: str) -> int:
    if re.fullmatch(r"\$[0-9]+(?:\.[0-9]{1,6})?", text) is None:
        raise RuntimeError("maple: expected explicit USD/M token price")
    value = int(Decimal(text[1:]) * 1_000_000)
    if value > 10**12:
        raise RuntimeError("maple: token price out of range")
    return value


def parse_prices(html: str) -> ModelPrice:
    soup = BeautifulSoup(html, "html.parser")
    if "USD equivalent per 1 million tokens." not in soup.get_text(" ", strip=True):
        raise RuntimeError("maple: token price currency or unit changed")
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
            if values["model"] != DISPLAY_NAME:
                continue
            prompt = _price(values["input"])
            cached = _price(values["cached input"])
            completion = _price(values["output"])
            if not prompt or not completion or cached > prompt:
                raise RuntimeError("maple: invalid token prices")
            prices.append(ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached))
    if len(prices) != 1:
        raise RuntimeError("maple: release-pinned model price missing or duplicated")
    return prices[0]


def parse_context(html: str) -> int:
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
            if NATIVE_ID not in codes:
                continue
            text = values["context tokens"].get_text(strip=True)
            if codes != [NATIVE_ID] or re.fullmatch(r"[1-9][0-9]*(?:,[0-9]{3})*", text) is None:
                raise RuntimeError("maple: ambiguous model or context")
            context = int(text.replace(",", ""))
            if not 1024 <= context <= 1_048_576:
                raise RuntimeError("maple: context requires release review")
            contexts.append(context)
    if len(contexts) != 1:
        raise RuntimeError("maple: release-pinned model capability missing or duplicated")
    return contexts[0]


def fetch() -> ProviderPricingResult:
    global _DISCOVERED  # noqa: PLW0603
    _DISCOVERED = {}
    price = parse_prices(fetch_html(PRICING_URL))
    context = parse_context(fetch_html(MODEL_DOCS_URL))
    row: dict[str, Any] = {
        "id": MODEL_ID, "upstream_id": NATIVE_ID, "display_name": DISPLAY_NAME,
        "context_length": context, "model_type": "chat",
        "input_modalities": ["text"], "output_modalities": ["text"],
        "endpoints": ["chat/completions"], "supports_tools": False,
        "supports_reasoning": False, "routable": MODEL_ID not in _PENDING_ENCLAVE_MODELS,
    }
    if not row["routable"]:
        row["routable_reason"] = "attested-enclave-rollout-pending"
    _DISCOVERED = {MODEL_ID: row}
    return ProviderPricingResult(
        slug=SLUG, prices={MODEL_ID: price}, source="html", fetched_url=PRICING_URL,
        notes=["Published token rates only; no inferred credit minimum or rounding fee. "
               "Only the release-pinned dedicated uncensored workload is eligible for review."],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if result.slug != SLUG or set(result.prices) != {MODEL_ID} or not _DISCOVERED:
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
