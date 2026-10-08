"""Direct Meta Standard-tier discovery and first-party pricing.

Contributor models allow training on customer content and are never admitted.
Non-chat model families require their own meters and adapters.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "meta"
BASE_URL = "https://api.meta.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://dev.meta.ai/docs/pricing-rate-limits"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/meta.json"
MANIFEST_STALE_FALLBACK = True
MODEL_ID = "meta/muse-spark-1.1"
EXPLICIT_MODEL_MAP = {f"muse-spark-{version}": f"meta/muse-spark-{version}" for version in ("1.1", "1.2", "1.3")}
EXPECTED_MODELS = list(EXPLICIT_MODEL_MAP.values())


def _parse_prices(html: str) -> dict[str, ModelPrice]:
    soup = BeautifulSoup(html, "html.parser")
    headings = [node for node in soup.find_all(["h2", "h3"]) if node.get_text(" ", strip=True) == "Standard tier"]
    if len(headings) != 1:
        raise RuntimeError("meta: ambiguous Standard-tier pricing section")
    section: list[Tag] = []
    for node in headings[0].next_siblings:
        if isinstance(node, Tag):
            if node.name in {"h2", "h3"}:
                break
            section.append(node)
    text = " ".join(node.get_text(" ", strip=True) for node in section)
    if "Price per 1M tokens" not in text:
        raise RuntimeError("meta: missing per-million pricing unit")
    native_ids = set(re.findall(r"\bmuse-spark-\d+\.\d+(?:-[a-z]+)?\b", text))
    if not set(EXPLICIT_MODEL_MAP) <= native_ids or any("contributor" in name for name in native_ids):
        raise RuntimeError("meta: unexpected Standard-tier model list")
    tables = [table for node in section for table in ([node] if node.name == "table" else node.find_all("table"))]
    if len(tables) != 1:
        raise RuntimeError("meta: ambiguous Standard-tier price table")
    amounts: dict[str, int] = {}
    for row in tables[0].find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        if len(cells) != 2:
            raise RuntimeError("meta: unexpected price row")
        label, raw = [cell.get_text(" ", strip=True) for cell in cells]
        if label not in {"Cached input", "Input", "Output"} or label in amounts or not re.fullmatch(r"\$\d+(?:\.\d+)?", raw):
            raise RuntimeError("meta: malformed Standard-tier price")
        amount = Decimal(raw[1:]) * 1_000_000
        if amount <= 0 or amount != amount.to_integral_value():
            raise RuntimeError("meta: invalid price precision or amount")
        amounts[label] = int(amount)
    if set(amounts) != {"Cached input", "Input", "Output"} or amounts["Cached input"] > amounts["Input"]:
        raise RuntimeError("meta: incomplete or invalid Standard-tier rates")
    price = ModelPrice(amounts["Input"], amounts["Output"], prompt_cached_micro_per_m=amounts["Cached input"])
    return {model_id: price for model_id in EXPLICIT_MODEL_MAP.values()}


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # /models only returns IDs. Context/modalities are documented in /docs/models.
    # Advertise only the text/image input supported by our chat adapter.
    return [{**row, "context_length": 1_048_576, "input_modalities": ["text", "image"], "output_modalities": ["text"]} for row in rows if row.get("id") in EXPLICIT_MODEL_MAP]


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="META_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        model_id_resolver=EXPLICIT_MODEL_MAP.get,
        expected_models=tuple(EXPECTED_MODELS),
        price_loader=lambda: _parse_prices(fetch_html(PRICING_URL)),
        pricing_source_url=PRICING_URL, normalize_rows=_normalize_rows,
        canary_max_tokens=512, canary_expected_content="PONG",
        canary_require_usage=True, canary_require_message=True,
        canary_extra_body={"reasoning_effort": "minimal"},
    ), manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
