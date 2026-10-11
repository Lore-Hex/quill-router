"""Zro live availability joined to its public USD price table, without private aliases."""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "zro"
BASE_URL = "https://zro.moonmath.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://zro.moonmath.ai/docs/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/zro.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def canonical_model_id(native_id: str) -> str | None:
    native_id = native_id.removeprefix("zro/")
    if native_id == "auto" or native_id.endswith("-private"):
        return None  # Dynamic pricing or non-public weights; never conflate them.
    return canonicalize_unqualified_model_id(native_id) or f"zro/{native_id}"


def _usd(value: str) -> int:
    if not re.fullmatch(r"\$[0-9]+(?:\.[0-9]+)?", value):
        raise RuntimeError("zro: expected exact USD price")
    amount = Decimal(value[1:]) * 1_000_000
    if amount != amount.to_integral_value():
        raise RuntimeError("zro: unsupported price precision")
    return int(amount)


def parse_prices(html: str) -> dict[str, tuple[ModelPrice, int, str]]:
    soup = BeautifulSoup(html, "html.parser")
    if "Prices are USD per 1M tokens" not in soup.get_text(" ", strip=True):
        raise RuntimeError("zro: pricing units changed")
    tables = [table for table in soup.select("table") if [h.get_text(strip=True) for h in table.select("th")] == ["Model", "Context", "Input", "Output", "Cache read"]]
    if len(tables) != 1:
        raise RuntimeError("zro: expected one model price table")
    result: dict[str, tuple[ModelPrice, int, str]] = {}
    for tr in tables[0].select("tbody tr"):
        cells = tr.select("td")
        identity = cells[0].select_one("code") if cells else None
        title = cells[0].select_one("a") if cells else None
        if len(cells) != 5 or identity is None or title is None:
            raise RuntimeError("zro: malformed price row")
        native = identity.get_text(strip=True)
        if not native or native in result:
            raise RuntimeError("zro: duplicate or invalid price model")
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(K|M)", cells[1].get_text(strip=True))
        if not match:
            raise RuntimeError("zro: invalid context length")
        context = int(Decimal(match[1]) * (1_000 if match[2] == "K" else 1_000_000))
        prompt, output, cached = [_usd(cell.get_text(strip=True)) for cell in cells[2:]]
        if context <= 0 or prompt <= 0 or output <= 0 or cached > prompt:
            raise RuntimeError("zro: invalid context or prices")
        result[native] = (ModelPrice(prompt, output, prompt_cached_micro_per_m=cached), context, title.get_text(strip=True))
    if not result:
        raise RuntimeError("zro: empty price table")
    return result


def discover(payload: object, html: str) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("zro: catalog has no data list")
    price_table = parse_prices(html)
    native_ids: set[str] = set()
    for source in payload["data"]:
        native = source.get("id") if isinstance(source, dict) else None
        if not isinstance(native, str) or not native or native in native_ids:
            raise RuntimeError("zro: invalid or duplicate catalog ID")
        native_ids.add(native)
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for native in sorted(native_ids):
        bare = native.removeprefix("zro/")
        if native.startswith("zro/") and bare in native_ids:
            continue
        model_id = canonical_model_id(native)
        if model_id is None:
            continue
        if model_id in rows:
            raise RuntimeError("zro: duplicate canonical ID")
        row: dict[str, Any] = {"id": model_id, "upstream_id": native, "endpoints": ["chat/completions"]}
        if bare in price_table:
            price, context, title = price_table[bare]
            prices[model_id] = price
            row.update(display_name=title, context_length=context, input_modalities=["text"], output_modalities=["text"],
                       supported_features=["chat", "completion", "streaming", "tools", "reasoning", "prompt_caching"])
        else:
            # The API still advertises 0731 but the public docs retire it.
            reason = "provider-retired" if bare == "deepseek-v4-flash-0731" else "awaiting-price"
            row.update(routable=False, routable_reason=reason)
        rows[model_id] = row
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("zro: no priced live models" if not prices else "; ".join(errors))
    return prices, rows


def fetch() -> ProviderPricingResult:
    _DISCOVERED_ROWS.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("ZRO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ZRO_API_KEY is required for discovery")
    prices, rows = discover(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}), fetch_html(PRICING_URL))
    checked = models_requiring_canary(MANIFEST_PATH, prices)
    healthy = {model_id for model_id in checked if probe_openai_chat(
        base_url=BASE_URL, api_key=key, model=rows[model_id]["upstream_id"],
        max_tokens=512, expected_content="PONG", require_usage=True,
        prompt="Reply with exactly PONG and nothing else.",
    )}
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    _DISCOVERED_ROWS.update(rows)
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    return ProviderPricingResult(slug=SLUG, prices=prices, source="api", fetched_url=PRICING_URL,
                                 notes=[f"{len(prices)} priced models; {len(healthy)}/{len(checked)} canaries passed"])


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("zro: fetch must succeed before writing manifest")
    return write_discovered_chat_manifest(result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS,
                                         source_url=URL, pricing_source_url=PRICING_URL)
