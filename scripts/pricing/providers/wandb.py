"""W&B Inference availability joined to first-party model prices."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, fetch_html
from scripts.pricing.model_ids import mapped_or_canonical_model_id
from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "wandb"
BASE_URL = "https://api.inference.wandb.ai/v1"
URL = f"{BASE_URL}/models"
MODEL_DOCS_URL = "https://docs.wandb.ai/inference/models"
PRICING_URL = "https://wandb.ai/site/pricing/inference/"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/wandb.json"
)
MANIFEST_STALE_FALLBACK = True

_MODEL_TABLE_HEADERS = (
    "Model",
    "Model ID (for API usage)",
    "Type",
    "Context Window",
    "Parameters",
    "Description",
)
_PRICE_TABLE_HEADERS = ("Model", "Input Tokens", "Output Tokens", "Cache Hit")
_CONTEXT_LENGTHS = {
    "32.8k": 32_768,
    "128k": 131_072,
    "131k": 131_072,
    "161k": 163_840,
    "197k": 196_608,
    "203k": 202_752,
    "262k": 262_144,
    "1049k": 1_048_576,
}
_DOLLAR_RE = re.compile(r"^\$\s*([0-9]+(?:\.[0-9]+)?)$")


def _headers(table: Any) -> tuple[str, ...]:
    return tuple(cell.get_text(" ", strip=True) for cell in table.find_all("th"))


def _parse_model_docs(html: str) -> dict[str, dict[str, Any]]:
    """Read exact API IDs and capabilities from W&B's model tables."""

    models: dict[str, dict[str, Any]] = {}
    labels: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        if _headers(table) != _MODEL_TABLE_HEADERS:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) != len(_MODEL_TABLE_HEADERS):
                continue
            label = cells[0].get_text(" ", strip=True)
            native_id = cells[1].get_text(" ", strip=True)
            modalities = {
                value.strip().casefold()
                for value in cells[2].get_text(" ", strip=True).split(",")
                if value.strip()
            }
            context_text = cells[3].get_text(" ", strip=True).casefold()
            if not label or not native_id or "text" not in modalities:
                continue
            context_length = _CONTEXT_LENGTHS.get(context_text)
            if context_length is None:
                raise RuntimeError(
                    f"wandb: unsupported context window {context_text!r} for {native_id}"
                )
            if native_id in models or label in labels:
                raise RuntimeError(f"wandb: duplicate documented model {native_id!r}")
            labels.add(label)
            input_modalities = ["text"]
            if "vision" in modalities or "image" in modalities:
                input_modalities.append("image")
            models[native_id] = {
                "label": label,
                "name": label,
                "context_length": context_length,
                "input_modalities": input_modalities,
                "output_modalities": ["text"],
            }
    if len(models) < 10:
        raise RuntimeError("wandb: official model documentation returned fewer than 10 models")
    return models


def _microdollars_per_million(text: str) -> int | None:
    value = text.strip()
    if not value or value == "-":
        return None
    match = _DOLLAR_RE.fullmatch(value)
    if match is None:
        raise RuntimeError(f"wandb: invalid price {text!r}")
    try:
        dollars = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise RuntimeError(f"wandb: invalid price {text!r}") from exc
    if not dollars.is_finite() or dollars <= 0:
        raise RuntimeError(f"wandb: non-positive price {text!r}")
    return int((dollars * Decimal(1_000_000)).to_integral_value(ROUND_HALF_UP))


def _parse_prices(
    html: str,
    *,
    documented_models: dict[str, dict[str, Any]],
) -> dict[str, ModelPrice]:
    """Parse only the W&B-hosted inference table, excluding third-party rows."""

    label_to_native_id = {
        str(model["label"]): native_id for native_id, model in documented_models.items()
    }
    prices: dict[str, ModelPrice] = {}
    soup = BeautifulSoup(html, "html.parser")
    if "Prices shown are per 1 million tokens." not in soup.get_text(" ", strip=True):
        raise RuntimeError("wandb: pricing page no longer declares per-million-token units")
    for comparison in soup.select("div.compare-table"):
        header = comparison.find("table", attrs={"data-compare": "header-table"})
        body = comparison.find("table", attrs={"data-compare": "body-table"})
        if header is None or body is None or _headers(header) != _PRICE_TABLE_HEADERS:
            continue
        for row in body.select("tr.compare-data-row"):
            label_cell = row.select_one("[data-compare='row-label']")
            cells = row.find_all("td", recursive=False)
            if label_cell is None or len(cells) != 3:
                continue
            native_id = label_to_native_id.get(label_cell.get_text(" ", strip=True))
            if native_id is None:
                continue
            model_id = mapped_or_canonical_model_id(native_id, {})
            if model_id is None:
                continue
            prompt = _microdollars_per_million(cells[0].get_text(" ", strip=True))
            completion = _microdollars_per_million(cells[1].get_text(" ", strip=True))
            cached = _microdollars_per_million(cells[2].get_text(" ", strip=True))
            if prompt is None or completion is None:
                raise RuntimeError(f"wandb: incomplete price for {native_id}")
            if model_id in prices:
                raise RuntimeError(f"wandb: duplicate price for {model_id}")
            prices[model_id] = ModelPrice(
                prompt,
                completion,
                prompt_cached_micro_per_m=cached,
            )
    if len(prices) < 10:
        raise RuntimeError("wandb: official pricing page returned fewer than 10 model prices")
    return prices


@cache
def _load_model_docs() -> dict[str, dict[str, Any]]:
    return _parse_model_docs(fetch_html(MODEL_DOCS_URL))


def _load_prices() -> dict[str, ModelPrice]:
    return _parse_prices(
        fetch_html(PRICING_URL),
        documented_models=_load_model_docs(),
    )


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documented = _load_model_docs()
    normalized: list[dict[str, Any]] = []
    for source in rows:
        native_id = source.get("id")
        if not isinstance(native_id, str):
            continue
        model = documented.get(native_id)
        if model is None:
            normalized.append(dict(source))
            continue
        row = dict(source)
        row.update(model)
        normalized.append(row)
    if len(normalized) < 10:
        raise RuntimeError("wandb: authenticated catalog returned fewer than 10 models")
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        catalog_url=URL,
        api_key_env="WANDB_API_KEY",
        explicit_model_map={},
        pricing_source_url=PRICING_URL,
        price_loader=_load_prices,
        normalize_rows=_normalize_rows,
        canary_max_tokens=16,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
