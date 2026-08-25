"""Perplexity authenticated OpenAI-compatible native model catalog."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import ProviderPricingResult, fetch_html
from scripts.pricing.manifest import guard_fixed_output_prices
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "perplexity"
BASE_URL = "https://api.perplexity.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://docs.perplexity.ai/docs/getting-started/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/perplexity.json"
)
MANIFEST_STALE_FALLBACK = True


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Perplexity's USD/M catalog unit to the shared USD/token unit."""

    normalized: list[dict[str, Any]] = []
    for source in rows:
        pricing = source.get("pricing")
        if not isinstance(pricing, dict) or pricing.get("unit") != "usd_per_1m_tokens":
            continue
        try:
            prompt = Decimal(str(pricing["input"])) / Decimal(1_000_000)
            completion = Decimal(str(pricing["output"])) / Decimal(1_000_000)
            cached_raw = pricing.get("cache_read")
            cached = (
                Decimal(str(cached_raw)) / Decimal(1_000_000) if cached_raw is not None else None
            )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            continue
        row = dict(source)
        # Perplexity's authenticated catalog namespaces Sonar, but the native
        # /v1/sonar endpoint rejects that value and requires the bare ID.
        # Normalize before shared discovery so upstream_id remains `sonar`
        # while the public namespace is reconstructed as `perplexity/sonar`.
        if str(row.get("id", "")).strip().casefold() == "perplexity/sonar":
            row["id"] = "sonar"
        normalized_pricing = {
            "prompt": str(prompt),
            "completion": str(completion),
        }
        if cached is not None:
            normalized_pricing["input_cache_read"] = str(cached)
        row["pricing"] = normalized_pricing
        normalized.append(row)
    return normalized


def _is_perplexity_route(row: dict[str, Any]) -> bool:
    model_id = row.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return False
    normalized = model_id.strip().casefold()
    if normalized.startswith("perplexity/"):
        normalized = normalized.removeprefix("perplexity/")
    elif "/" in normalized:
        return False
    # The first release intentionally admits only plain Sonar. Sonar Pro has a
    # different fixed request fee, and Deep Research adds variable citation,
    # search-query, and reasoning charges that are not representable by this
    # fixed-fee adapter. Unknown and more complex rows remain dark.
    return normalized == "sonar"


def _parse_low_context_request_price(html: str) -> int:
    """Return Sonar's exact low-context fee in microdollars per request."""

    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table"):
        headers = [cell.get_text(" ", strip=True).casefold() for cell in table.select("thead th")]
        if not headers or "low context size" not in headers:
            continue
        low_index = headers.index("low context size")
        for row in table.select("tbody tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) <= low_index:
                continue
            if cells[0].get_text(" ", strip=True).casefold() != "sonar":
                continue
            match = re.fullmatch(
                r"\$([0-9]+(?:\.[0-9]+)?)",
                cells[low_index].get_text(" ", strip=True),
            )
            if match is None:
                break
            # The official table is dollars per 1,000 successful requests.
            return int((Decimal(match.group(1)) * Decimal(1000)).to_integral_exact())
    raise RuntimeError("perplexity: Sonar low-context request price not found")


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        catalog_url=URL,
        api_key_env="PERPLEXITY_API_KEY",
        explicit_model_map={},
        namespace_unqualified="perplexity",
        include=_is_perplexity_route,
        normalize_rows=_normalize_rows,
        pricing_source_url=PRICING_URL,
        canary_max_tokens=16,
        canary_expected_content="PONG",
        canary_endpoint_path="/sonar",
        canary_extra_body={"search_context_size": "low"},
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map


def fetch() -> ProviderPricingResult:
    result = CATALOG.fetch()
    request_price = _parse_low_context_request_price(fetch_html(PRICING_URL))
    for row in CATALOG.discovered_rows.values():
        row["fixed_request_price_microdollars"] = request_price
    guard_fixed_output_prices(MANIFEST_PATH, CATALOG.discovered_rows)
    return result


write_provider_manifest = CATALOG.write_provider_manifest
