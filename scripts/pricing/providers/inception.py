"""Inception Labs native catalog with official documentation price fallback."""

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "inception"
BASE_URL = "https://api.inceptionlabs.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://docs.inceptionlabs.ai/get-started/models.md"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/inception.json"
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {"mercury-2": "inception/mercury-2", "mercury-3": "inception/mercury-3"}


def _chat_row(row: dict[str, Any]) -> bool:
    endpoints = row.get("supported_endpoints")
    return isinstance(endpoints, list) and any(
        endpoint in {"chat.completions", "chat/completions", "v1/chat/completions"}
        for endpoint in endpoints if isinstance(endpoint, str)
    )


def _parse_documented_prices(markdown: str) -> dict[str, dict[str, str]]:
    prices: dict[str, dict[str, str]] = {}
    for line in markdown.replace(r"\$", "$").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 9 or "`v1/chat/completions`" not in cells[4]:
            continue
        name = cells[0].strip("*").lower()
        if not re.fullmatch(r"mercury [a-z0-9.]+", name):
            continue
        values = []
        for cell in cells[1:4]:
            # Discard crossed-out list prices; require one current USD rate.
            current = re.sub(r"~~.*?~~", "", cell)
            amounts = re.findall(r"\$([0-9]+(?:\.[0-9]+)?)", current)
            if len(amounts) != 1:
                raise ValueError(f"inception: ambiguous documentation price for {name}")
            values.append(str(Decimal(amounts[0]) / Decimal(1_000_000)))
        pricing = dict(zip(("prompt", "input_cache_reads", "completion"), values, strict=True))
        native_id = name.replace(" ", "-")
        if native_id in prices and prices[native_id] != pricing:
            raise ValueError(f"inception: conflicting documentation prices for {native_id}")
        prices[native_id] = pricing
    return prices


def _positive_native_prices(row: dict[str, Any]) -> bool:
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return False
    try:
        values = [Decimal(str(pricing[field])) for field in ("prompt", "completion")]
        return all(value.is_finite() and value > 0 for value in values)
    except (KeyError, InvalidOperation):
        return False


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chat_rows = [dict(row) for row in rows if _chat_row(row)]
    missing = [row for row in chat_rows if not _positive_native_prices(row)]
    if missing:
        # Launch-day /models reports Mercury 3 as free. Never publish that
        # placeholder or freeze a promotion: re-read the official tariff.
        documented = _parse_documented_prices(fetch_html(PRICING_URL))
        for row in missing:
            native_id = row.get("id")
            if isinstance(native_id, str) and native_id in documented:
                row["pricing"] = documented[native_id]
    return chat_rows


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="INCEPTION_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        namespace_unqualified=SLUG,
        expected_models=("inception/mercury-3",),
        normalize_rows=_normalize_rows,
        pricing_source_url=PRICING_URL,
        canary_max_tokens=256,
        canary_expected_content="PONG",
        canary_require_usage=True,
        canary_require_message=True,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
