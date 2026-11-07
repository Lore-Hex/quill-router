"""Sakana AI authenticated catalog with first-party exact token prices."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, PriceTier, fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec
from trusted_router.provider_contracts import (
    SAKANA_FUGU_MODEL_ID,
    SAKANA_NAMAZU_MODEL_ID,
    SAKANA_NAMAZU_ROUTE_HOLD_REASON,
)
from trusted_router.provider_lifecycle import provider_price_microdollars

SLUG = "sakana"
BASE_URL = "https://api.sakana.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://console.sakana.ai/pricing"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/sakana.json"
MANIFEST_STALE_FALLBACK = True

MODEL_MAP = {
    "fugu-ultra-v1.1": SAKANA_FUGU_MODEL_ID,
    "sakana-namazu-v1.0": SAKANA_NAMAZU_MODEL_ID,
}
EXPECTED_MODELS = (SAKANA_FUGU_MODEL_ID, SAKANA_NAMAZU_MODEL_ID)


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


_JST = timezone(timedelta(hours=9), "JST")
_SCHEDULED_HEADER = re.compile(r"From ([A-Z][a-z]{2}) ([0-9]{1,2}), ([0-9]{4}), ([0-9]{1,2}):([0-9]{2}) JST")


def _scheduled_at(header: str) -> datetime | None:
    match = _SCHEDULED_HEADER.fullmatch(header)
    if match is None:
        return None
    month, day, year, hour, minute = match.groups()
    try:
        local = datetime.strptime(f"{year} {month} {day} {hour} {minute}", "%Y %b %d %H %M")
    except ValueError:
        return None
    return local.replace(tzinfo=_JST).astimezone(UTC)


def _column_price(rows: dict[str, list[int]], column: int) -> ModelPrice:
    return ModelPrice(
        prompt_micro_per_m=rows["input"][column],
        completion_micro_per_m=rows["output"][column],
        prompt_cached_micro_per_m=rows["cached input"][column],
    )


def _matches_announced(price: ModelPrice, at: datetime) -> bool:
    announced = provider_price_microdollars(SLUG, SAKANA_NAMAZU_MODEL_ID, at=at)
    tier = price.tiers[0]
    return announced is not None and (
        announced.prompt_microdollars_per_million_tokens,
        announced.completion_microdollars_per_million_tokens,
        announced.prompt_cached_microdollars_per_million_tokens,
    ) == (tier.prompt_micro_per_m, tier.completion_micro_per_m, tier.prompt_cached_micro_per_m)


def _parse_namazu_price(soup: BeautifulSoup, *, now: datetime | None = None) -> ModelPrice:
    namazu_table = _pricing_table_after(soup, "sakana-namazu-v1.0")
    namazu_headers = [
        cell.get_text(" ", strip=True) for cell in namazu_table.select("thead th")
    ]
    if namazu_headers == ["Token type", "Price"]:
        return _column_price(_token_price_rows(namazu_table, expected_columns=2), 0)
    # 2026-11: the page shows the current price beside an announced one
    # ("Current" | "From Dec 1, 2026, 10:00 JST"). Both columns must equal
    # the cutover recorded in provider_lifecycle, which reprices routes at
    # authorization time; an unrecorded announcement fails closed.
    scheduled_at = _scheduled_at(namazu_headers[2]) if len(namazu_headers) == 3 else None
    if namazu_headers[:2] != ["Token type", "Current"] or scheduled_at is None:
        raise RuntimeError(f"sakana: Namazu pricing headers changed: {namazu_headers!r}")
    rows = _token_price_rows(namazu_table, expected_columns=3)
    current, scheduled = _column_price(rows, 0), _column_price(rows, 1)
    if not (
        _matches_announced(current, scheduled_at - timedelta(seconds=1))
        and _matches_announced(scheduled, scheduled_at)
    ):
        raise RuntimeError(
            f"sakana: Namazu price change from {scheduled_at.isoformat()} is not recorded in provider_lifecycle"
        )
    return scheduled if (now or datetime.now(UTC)) >= scheduled_at else current


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


def _parse_pricing(html: str, *, now: datetime | None = None) -> dict[str, ModelPrice]:
    """Strictly parse every published Sakana chat price for regression tests."""

    soup = BeautifulSoup(html, "html.parser")
    prices = {SAKANA_NAMAZU_MODEL_ID: _parse_namazu_price(soup, now=now)}
    fugu = _parse_fugu_price(soup)
    if fugu is not None:
        prices[SAKANA_FUGU_MODEL_ID] = fugu
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    prices = _parse_pricing(fetch_html(PRICING_URL))
    if SAKANA_FUGU_MODEL_ID not in prices:
        raise RuntimeError("sakana: live Fugu pricing disappeared")
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
            SAKANA_NAMAZU_MODEL_ID: SAKANA_NAMAZU_ROUTE_HOLD_REASON,
        },
        # Fugu can spend a small response budget entirely on private reasoning,
        # yielding a healthy but empty length-truncated response. A 256-token
        # ceiling remains cheap while reliably reaching the visible PONG.
        canary_max_tokens=256,
        canary_expected_content="PONG",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
