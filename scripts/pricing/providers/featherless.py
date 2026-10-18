"""Featherless current-plan chat catalog with exact authenticated prices."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    emit_workflow_warning,
    fetch_html,
    fetch_json,
    read_stale_provider_manifest,
)
from scripts.pricing.openai_catalog import dollars_per_token_to_micro_per_m, openai_model_price
from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "featherless"
BASE_URL = "https://api.featherless.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://featherless.ai/docs/request-pricing-and-credits"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/featherless.json"
)
MANIFEST_STALE_FALLBACK = True

CURATED_NATIVE_MODELS = (
    "deepseek-ai/DeepSeek-V4.1-Flash",
    "deepseek-ai/DeepSeek-V4-Flash-0731",
    "moonshotai/Kimi-K3",
    "Qwen/Qwen3.8-Flash-Next",
    "zai-org/GLM-5.2",
    "zai-org/GLM-5.3",
    "zai-org/GLM-5.3-Flash",
)

# Featherless serves tens of thousands of community fine-tunes. Scan a bounded
# release window, but only admit first-party model publishers we route elsewhere.
# Recheck committed routes independently of this window. Required models above
# remain fail-closed; other committed routes use the normal delisting policy.
DISCOVERY_NATIVE_OWNERS = frozenset(
    {
        "deepseek-ai",
        "MiniMaxAI",
        "moonshotai",
        "Qwen",
        "stepfun-ai",
        "XiaomiMiMo",
        "zai-org",
    }
)
DISCOVERY_PAGE_SIZE = 1000
_PRESERVED_NATIVE_IDS: set[str] = set()


def _is_discovery_candidate(row: dict[str, Any]) -> bool:
    native_id = row.get("id")
    return (
        isinstance(native_id, str)
        and native_id.partition("/")[0] in DISCOVERY_NATIVE_OWNERS
        and row.get("available_on_current_plan") is True
    )


def _committed_native_models() -> list[str]:
    if not CATALOG.manifest_path.exists():
        return []
    raw = json.loads(CATALOG.manifest_path.read_text(encoding="utf-8"))
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("featherless: committed manifest has no models list")
    native_ids = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("featherless: invalid committed model row")
        if row.get("routable") is False:
            continue
        native_id = row.get("upstream_id")
        if not isinstance(native_id, str) or not native_id.strip():
            raise RuntimeError("featherless: routable committed model has no upstream id")
        native_ids.append(native_id)
    return native_ids


def _load_rows(api_key: str) -> list[dict[str, Any]]:
    _PRESERVED_NATIVE_IDS.clear()
    query = urlencode(
        {
            "available_on_current_plan": "true",
            "status": "active",
            "sort": "-hf_created_at",
            "page": "1",
            "per_page": str(DISCOVERY_PAGE_SIZE),
        }
    )
    listing = fetch_json(
        f"{URL}?{query}",
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        raise RuntimeError("featherless: invalid paginated model catalog")

    rows_by_id = {
        str(row["id"]): row
        for row in listing["data"]
        if isinstance(row, dict) and _is_discovery_candidate(row)
    }
    for native_id in dict.fromkeys((*CURATED_NATIVE_MODELS, *_committed_native_models())):
        try:
            payload = fetch_json(
                f"{URL}/{quote(native_id, safe='')}",
                extra_headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404 or native_id in CURATED_NATIVE_MODELS:
                raise
            rows_by_id.pop(native_id, None)
            continue
        if not isinstance(payload, dict):
            raise RuntimeError(f"featherless: invalid model detail for {native_id}")
        available = payload.get("available_on_current_plan")
        if available is not True:
            if native_id in CURATED_NATIVE_MODELS:
                raise RuntimeError(f"featherless: {native_id} is not available on current plan")
            if available is not False:
                _PRESERVED_NATIVE_IDS.add(native_id)
                emit_workflow_warning(
                    f"featherless: malformed availability for {native_id}; "
                    "keeping committed prices and routability for this run"
                )
            # A fresh detail supersedes a stale listing. The shared writer
            # counts only explicit false/404 as a miss; fetch restores rows
            # whose availability is unknown before the writer sees them.
            rows_by_id.pop(native_id, None)
            continue
        rows_by_id[native_id] = payload
    return list(rows_by_id.values())


def _table_price(text: str) -> int:
    match = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)(?:\s*/\s*1M\s+tok(?:ens)?)?", text)
    if match is None:
        raise RuntimeError(f"featherless: invalid token price {text!r}")
    value = dollars_per_token_to_micro_per_m(Decimal(match[1]) / Decimal(1_000_000))
    assert value is not None
    return value


def _published_prices(html: str) -> tuple[dict[str, ModelPrice], dict[str, ModelPrice]]:
    """Read named columns; exact model overrides take precedence over class rates."""
    classes: dict[str, ModelPrice] = {}
    models: dict[str, ModelPrice] = {}
    tables = BeautifulSoup(html, "html.parser").find_all("table")
    if not tables:
        # The docs now defer effective prices to model pages (2026-10-11).
        # Without published cache rates, use only the authenticated API prices.
        # Do not treat existing tables with missing/changed columns as absent.
        emit_workflow_warning(
            "featherless: no published cache prices (pricing page has no tables); "
            "using authenticated API prices without an inferred cache discount"
        )
        return classes, models
    seen_tables: set[str] = set()
    required = {"model class", "input", "cached input / 1m tokens", "output / 1m tokens"}
    for table in tables:
        headers = [
            " ".join(cell.get_text(" ", strip=True).casefold().split())
            for cell in table.find_all("th")
        ]
        if not required <= set(headers):
            continue
        key = "model" if "model" in headers else "model class"
        target = models if key == "model" else classes
        seen_tables.add(key)
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            if len(cells) != len(headers):
                raise RuntimeError("featherless: malformed cache price table row")
            row = dict(
                zip(headers, (cell.get_text(" ", strip=True) for cell in cells), strict=True)
            )
            if "chars" in row["input"] or row["output / 1m tokens"] == "n/a":
                continue
            cached_text = row["cached input / 1m tokens"]
            cached = None if cached_text == "-" else _table_price(cached_text)
            price = ModelPrice(
                _table_price(row["input"]),
                _table_price(row["output / 1m tokens"]),
                prompt_cached_micro_per_m=cached,
            )
            if cached is not None and cached > price.prompt_micro_per_m:
                raise RuntimeError(f"featherless: cache price exceeds input price for {row[key]}")
            if row[key] in target and target[row[key]] != price:
                raise RuntimeError(f"featherless: conflicting published prices for {row[key]}")
            target[row[key]] = price
    if seen_tables != {"model", "model class"} or not classes:
        raise RuntimeError("featherless: missing class/model cache price tables")
    return classes, models


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classes, models = _published_prices(fetch_html(PRICING_URL))
    normalized: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        features = source.get("features")
        row["supported_features"] = (
            ["tools"] if isinstance(features, dict) and features.get("tool_use") is True else []
        )
        # Model detail can advertise image-text-to-text while the legacy
        # vision_supported flag still says false (V4.1 Flash, verified live).
        tasks = source.get("tasks")
        if isinstance(tasks, list) and "image-text-to-text" in tasks:
            modalities = source.get("input_modalities")
            row["input_modalities"] = list(
                dict.fromkeys(["text", *(modalities if isinstance(modalities, list) else []), "image"])
            )
        normalized.append(row)
        if not isinstance(source.get("pricing"), dict):
            continue
        # The API's prompt/completion are USD/token; input/output are USD/M.
        # Keep the authenticated rates, and preserve explicit numeric zeroes.
        pricing = {
            key: str(value) if value is not None else None
            for key, value in source["pricing"].items()
        }
        if pricing.get("prompt") is None or pricing.get("completion") is None:
            raise RuntimeError(f"featherless: missing per-token API prices for {row.get('id')}")
        row["pricing"] = pricing
        current = openai_model_price(row)
        if current is None:
            continue
        if any(
            pricing.get(key) is not None
            for key in ("input_cache_read", "input_cache_reads", "cache_read")
        ):
            cached = current.tiers[0].prompt_cached_micro_per_m
            if cached is None or cached > current.prompt_micro_per_m:
                raise RuntimeError(f"featherless: invalid API cache price for {row.get('id')}")
            continue
        published = models.get(str(row.get("id")), classes.get(str(row.get("model_class"))))
        if published is None or published.tiers[0].prompt_cached_micro_per_m is None:
            continue
        if (current.prompt_micro_per_m, current.completion_micro_per_m) != (
            published.prompt_micro_per_m,
            published.completion_micro_per_m,
        ):
            raise RuntimeError(
                f"featherless: authenticated/published price mismatch for {row.get('id')}"
            )
        pricing["input_cache_read"] = str(
            Decimal(published.tiers[0].prompt_cached_micro_per_m) / Decimal(1_000_000_000_000)
        )
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="FEATHERLESS_API_KEY",
        explicit_model_map={},
        expected_models=(
            "deepseek/deepseek-v4.1-flash",
            "deepseek/deepseek-v4-flash-0731",
            "moonshotai/kimi-k3",
            "qwen/qwen3.8-flash-next",
            "z-ai/glm-5.2",
            "z-ai/glm-5.3",
            "z-ai/glm-5.3-flash",
        ),
        catalog_url=URL,
        catalog_loader=_load_rows,
        normalize_rows=_normalize_rows,
        pricing_source_url=PRICING_URL,
        canary_max_tokens=32,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
write_provider_manifest = CATALOG.write_provider_manifest


def fetch() -> ProviderPricingResult:
    result = CATALOG.fetch()
    if _PRESERVED_NATIVE_IDS:
        committed, error = read_stale_provider_manifest(
            slug=SLUG, manifest_path=CATALOG.manifest_path, include_in_price_index=True,
        )
        if committed is None:
            raise RuntimeError(f"featherless: cannot preserve committed prices: {error}")
        rows = json.loads(CATALOG.manifest_path.read_text(encoding="utf-8"))["models"]
        for row in rows:
            if row.get("upstream_id") in _PRESERVED_NATIVE_IDS:
                model_id = row["id"]
                result.prices[model_id] = committed.prices[model_id]
                CATALOG.discovered_rows[model_id] = dict(row)
                CATALOG.upstream_id_map[model_id] = row["upstream_id"]
    return result
