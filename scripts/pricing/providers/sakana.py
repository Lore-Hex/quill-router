"""Sakana AI authenticated catalog with first-party exact token prices."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, PriceTier, fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec
from trusted_router.provider_contracts import (
    SAKANA_FUGU_MODEL_ID,
    SAKANA_FUGU_ROUTE_HOLD_REASON,
)

SLUG = "sakana"
BASE_URL = "https://api.sakana.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://console.sakana.ai/pricing"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/sakana.json"
MANIFEST_STALE_FALLBACK = True

MODEL_MAP = {
    "fugu-ultra-v1.1": SAKANA_FUGU_MODEL_ID,
    "sakana-namazu-v1.0": "sakana-ai/sakana-namazu-v1.0",
}
EXPECTED_MODELS = ("sakana-ai/sakana-namazu-v1.0",)
logger = logging.getLogger("pricing")


def _usd_per_m_to_micro(raw: str) -> int:
    match = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)", raw.strip())
    if match is None:
        raise RuntimeError(f"sakana: malformed USD-per-million price {raw!r}")
    return int(Decimal(match.group(1)) * Decimal(1_000_000))


def _pricing_table_after(soup: BeautifulSoup, heading: str) -> Tag:
    node = next(
        (
            candidate
            for candidate in soup.find_all(re.compile(r"^h[1-6]$"))
            if candidate.get_text(" ", strip=True) == heading
        ),
        None,
    )
    if node is None:
        raise RuntimeError(f"sakana: pricing page is missing {heading!r}")
    table = node.find_next_sibling("table")
    if not isinstance(table, Tag):
        raise RuntimeError(f"sakana: pricing page is missing the {heading!r} table")
    return table


def _token_price_rows(table: Tag, *, expected_columns: int) -> dict[str, list[int]]:
    rows: dict[str, list[int]] = {}
    for row in table.select("tbody tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if len(cells) != expected_columns:
            raise RuntimeError("sakana: pricing table column count changed")
        rows[cells[0].casefold()] = [_usd_per_m_to_micro(value) for value in cells[1:]]
    required = {"input", "output", "cached input"}
    if set(rows) != required:
        raise RuntimeError(
            f"sakana: pricing table token rows changed: {sorted(rows)!r}"
        )
    return rows


def _parse_namazu_price(soup: BeautifulSoup) -> ModelPrice:
    namazu_table = _pricing_table_after(soup, "sakana-namazu-v1.0")
    namazu_headers = [
        cell.get_text(" ", strip=True) for cell in namazu_table.select("thead th")
    ]
    if namazu_headers != ["Token type", "Price"]:
        raise RuntimeError(f"sakana: Namazu pricing headers changed: {namazu_headers!r}")
    namazu = _token_price_rows(namazu_table, expected_columns=2)
    return ModelPrice(
        prompt_micro_per_m=namazu["input"][0],
        completion_micro_per_m=namazu["output"][0],
        prompt_cached_micro_per_m=namazu["cached input"][0],
    )


def _parse_fugu_price(soup: BeautifulSoup) -> ModelPrice | None:
    fugu_heading = next(
        (
            candidate
            for candidate in soup.find_all(re.compile(r"^h[1-6]$"))
            if candidate.get_text(" ", strip=True) == "Fugu Ultra"
        ),
        None,
    )
    if fugu_heading is None:
        return None

    normalized_text = " ".join(soup.get_text(" ", strip=True).casefold().split())
    usage_contract = (
        "tokens from the user input sent to the first model",
        "sum of all input tokens used for orchestration",
        "output tokens from the orchestration",
    )
    if any(fragment not in normalized_text for fragment in usage_contract):
        raise RuntimeError("sakana: additive orchestration usage contract changed")

    fugu_table = _pricing_table_after(soup, "Fugu Ultra")
    headers = [cell.get_text(" ", strip=True) for cell in fugu_table.select("thead th")]
    if len(headers) != 3 or headers[:2] != ["Token type", "Standard price"]:
        raise RuntimeError(f"sakana: Fugu Ultra pricing headers changed: {headers!r}")
    threshold_match = re.fullmatch(r"Context\s*>\s*([0-9]+)K", headers[2])
    if threshold_match is None:
        raise RuntimeError(f"sakana: Fugu Ultra context tier changed: {headers[2]!r}")
    threshold = int(threshold_match.group(1)) * 1_000
    fugu = _token_price_rows(fugu_table, expected_columns=3)
    return ModelPrice(
        tiers=[
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_micro_per_m=fugu["input"][0],
                completion_micro_per_m=fugu["output"][0],
                prompt_cached_micro_per_m=fugu["cached input"][0],
            ),
            PriceTier(
                max_prompt_tokens=None,
                prompt_micro_per_m=fugu["input"][1],
                completion_micro_per_m=fugu["output"][1],
                prompt_cached_micro_per_m=fugu["cached input"][1],
            ),
        ]
    )


def _parse_pricing(html: str) -> dict[str, ModelPrice]:
    """Strictly parse every published Sakana chat price for regression tests."""

    soup = BeautifulSoup(html, "html.parser")
    prices = {"sakana-ai/sakana-namazu-v1.0": _parse_namazu_price(soup)}
    fugu = _parse_fugu_price(soup)
    if fugu is not None:
        prices[SAKANA_FUGU_MODEL_ID] = fugu
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    soup = BeautifulSoup(fetch_html(PRICING_URL), "html.parser")
    prices = {"sakana-ai/sakana-namazu-v1.0": _parse_namazu_price(soup)}
    try:
        fugu = _parse_fugu_price(soup)
    except RuntimeError as exc:
        # Fugu is deliberately operator-held. Its page drift must stay visible
        # without expiring the independently billable Namazu route.
        logger.warning("sakana: omitted held Fugu pricing after contract drift: %s", exc)
    else:
        if fugu is not None:
            prices[SAKANA_FUGU_MODEL_ID] = fugu
    return prices


def _include(row: dict[str, Any]) -> bool:
    """Admit only version-pinned routes with deterministic token pricing."""

    return row.get("id") in MODEL_MAP


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add first-party capability metadata omitted by Sakana's /models rows."""

    normalized: list[dict[str, Any]] = []
    for source in rows:
        native_id = source.get("id")
        if native_id not in MODEL_MAP:
            continue
        row = dict(source)
        if native_id == "fugu-ultra-v1.1":
            row.update(
                {
                    "context_length": 1_000_000,
                    "supported_features": [
                        "chat",
                        "completion",
                        "reasoning",
                        "tools",
                        "json_mode",
                        "structured_outputs",
                        "prompt_caching",
                    ],
                }
            )
        else:
            row.update(
                {
                    "context_length": 256_000,
                    "max_output_tokens": 65_536,
                    "input_modalities": ["text", "image"],
                    "supported_features": [
                        "chat",
                        "completion",
                        "reasoning",
                        "tools",
                        "vision",
                        "json_mode",
                        "structured_outputs",
                        "prompt_caching",
                    ],
                }
            )
        normalized.append(row)
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="SAKANA_API_KEY",
        explicit_model_map=MODEL_MAP,
        expected_models=EXPECTED_MODELS,
        pricing_source_url=PRICING_URL,
        price_loader=_load_prices,
        include=_include,
        normalize_rows=_normalize_rows,
        operator_hold_reasons={
            SAKANA_FUGU_MODEL_ID: SAKANA_FUGU_ROUTE_HOLD_REASON
        },
        canary_max_tokens=64,
        canary_expected_content="PONG",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
