"""StepFun authenticated chat catalog, official pricing, and live canaries."""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    fetch_html,
    validate,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import (
    discover_available_priced_chat_catalog,
    probe_openai_chat,
)

SLUG = "stepfun"
BASE_URL = "https://api.stepfun.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://platform.stepfun.ai/docs/en/guides/pricing/details"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "stepfun.json"
)

EXPLICIT_MODEL_MAP = {
    "step-3.5-flash": "stepfun/step-3.5-flash",
    "step-3.5-flash-2603": "stepfun/step-3.5-flash-2603",
    "step-3.7-flash": "stepfun/step-3.7-flash",
}
UPSTREAM_ID_MAP = {model_id: native_id for native_id, model_id in EXPLICIT_MODEL_MAP.items()}
EXPECTED_MODELS = list(EXPLICIT_MODEL_MAP.values())

_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
MANIFEST_STALE_FALLBACK = True


def _micro_per_m(raw: str) -> int:
    return int(Decimal(raw) * Decimal(1_000_000))


def _parse_pricing(html: str) -> dict[str, ModelPrice]:
    prices: dict[str, ModelPrice] = {}
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) != 5:
            continue
        native_id = cells[0].get_text(" ", strip=True)
        model_id = EXPLICIT_MODEL_MAP.get(native_id)
        if model_id is None or cells[1].get_text(" ", strip=True) != "1M tokens":
            continue
        values: list[int] = []
        for cell in cells[2:]:
            match = re.fullmatch(
                r"\$([0-9]+(?:\.[0-9]+)?)",
                cell.get_text(" ", strip=True),
            )
            if match is None:
                raise RuntimeError(f"stepfun: malformed pricing row for {native_id}")
            values.append(_micro_per_m(match.group(1)))
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=values[0],
            prompt_cached_micro_per_m=values[1],
            completion_micro_per_m=values[2],
        )
    return prices


def _parse_catalog(
    payload: object,
    prices: dict[str, ModelPrice],
) -> dict[str, dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("stepfun: authenticated /models response has no data list")
    discovered = discover_available_priced_chat_catalog(
        [row for row in rows if isinstance(row, dict)],
        prices=prices,
        explicit_map=EXPLICIT_MODEL_MAP,
        upstream_id_map=UPSTREAM_ID_MAP,
        include=lambda row: str(row.get("id") or "") in EXPLICIT_MODEL_MAP,
    )
    for model_id, row in discovered.items():
        row.update(
            {
                "model_type": "chat",
                "context_length": 262_144,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "supported_features": [
                    "chat",
                    "completion",
                    "reasoning",
                    "tools",
                    "json_mode",
                    "structured_outputs",
                    "prompt_caching",
                ],
                "status": 1,
            }
        )
        if model_id == "stepfun/step-3.7-flash":
            row["display_name"] = "Step 3.7 Flash"
        elif model_id == "stepfun/step-3.5-flash-2603":
            row["display_name"] = "Step 3.5 Flash 2603"
        else:
            row["display_name"] = "Step 3.5 Flash"
    return discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("STEPFUN_API_KEY")
    if not api_key:
        raise RuntimeError("stepfun: STEPFUN_API_KEY is required for discovery")
    prices = _parse_pricing(fetch_html(PRICING_URL))
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(
            URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": PROVIDER_FETCH_UA,
            },
        )
        response.raise_for_status()
        payload = response.json()

    discovered = _parse_catalog(payload, prices)
    checked = models_requiring_canary(MANIFEST_PATH, discovered)
    healthy = {
        model_id
        for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=UPSTREAM_ID_MAP[model_id],
            expected_content="PONG",
            # Step reasoning can consume the first few dozen output tokens.
            # A tiny canary would misclassify a healthy route as empty.
            max_tokens=256,
        )
    }
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered

    discovered_prices = {model_id: prices[model_id] for model_id in discovered}
    errors = validate(discovered_prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=discovered_prices,
        source="api",
        fetched_url=PRICING_URL,
        notes=[
            f"discovered {len(discovered)} priced StepFun chat models",
            f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
        pricing_source_url=PRICING_URL,
    )
