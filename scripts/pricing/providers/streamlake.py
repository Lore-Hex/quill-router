"""Discover StreamLake routes from its official catalog and USD price tables."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, fetch_html, fetch_provider
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "streamlake"
BASE_URL = "https://vanchin.streamlake.ai/api/gateway/v1/endpoints"
URL = "https://www.streamlake.ai/document/DOC/mgrnm4xm362hvp5wyce"
CATALOG_URL = "https://www.streamlake.ai/document/DOC/mh1gbfvrdn6hpbzxixv"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/streamlake.json"
MANIFEST_STALE_FALLBACK = True
EXPECTED_MODELS = ["z-ai/glm-5.3-flash", "deepseek/deepseek-v4.1-flash"]


def model_id(native: str) -> str | None:
    # Do not import helpers from an LLM-rewriteable price parser.
    value = native.strip().casefold().replace(" ", "-")
    if value.startswith("kat-coder-") and re.fullmatch(r"[a-z0-9._-]+", value):
        return f"kwaipilot/{value}"
    return canonicalize_unqualified_model_id(value)


def parse_catalog(html: str) -> list[dict[str, Any]]:
    rows = []
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        header: list[str] = []
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if "Context Length" in cells and "Name" in cells:
                header = cells
                continue
            if not header or len(cells) != len(header):
                continue
            values = dict(zip(header, cells, strict=True))
            native = values["Name"]
            if "retired" in native.casefold() or model_id(native) is None:
                continue
            match = re.fullmatch(r"(\d+)K", values["Context Length"], re.IGNORECASE)
            if not match:
                raise ValueError(f"streamlake: unknown context length for {native}")
            rows.append({
                "id": native,
                "name": native,
                "context_length": int(match[1]) * 1024,
                "input_modalities": ["text"] + (["image"] if "Image-to-Text" in values.get("Category", "") else []),
                "output_modalities": ["text"],
            })
    if not rows:
        raise ValueError("streamlake: official model catalog has no recognized rows")
    return rows


def _catalog_loader(_api_key: str) -> list[dict[str, Any]]:
    # The inference gateway has no OpenAI /models endpoint (Missing Action).
    return parse_catalog(fetch_html(CATALOG_URL))


def _prices() -> dict[str, ModelPrice]:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS).prices


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="STREAMLAKE_API_KEY",
        explicit_model_map={}, catalog_url=CATALOG_URL, catalog_loader=_catalog_loader,
        model_id_resolver=model_id,
        pricing_source_url=URL, price_loader=_prices,
        expected_models=tuple(EXPECTED_MODELS),
        canary_require_usage=True, canary_require_message=True, canary_max_tokens=32,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
