"""Lyceum's authenticated catalog and exact, currency-labelled USD token tariffs."""

from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "lyceum"
BASE_URL = "https://api.lyceum.technology/openai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = URL
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/lyceum.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}
# Reviewed on 2026-09-30: available aliases without an exact priced card.
# New unmatched models remain awaiting-price so the coverage gate reports them.
REVIEWED_UNPRICED_IDS = frozenset({
    "lyceum/complex", "lyceum/reasoning", "lyceum/router", "lyceum/simple",
    "minimax/minimax-m2.5", "qwen/qwen3-235b-a22b-2507",
    "qwen/qwen3.8-27b-instant", "qwen/qwen3.8-flash-next-instant",
    "z-ai/glm-5.2-instant", "z-ai/glm-5.3-instant", "z-ai/glm-5.3-flash-instant",
})


def canonical_model_id(native_id: str) -> str | None:
    return mapped_or_canonical_model_id(native_id, {})


def _field(card: Tag, label: str) -> str | None:
    labels = card.find_all("span", string=lambda value: bool(value and value.strip() == label))
    if len(labels) > 1:
        raise RuntimeError(f"lyceum: duplicate {label} field")
    if not labels:
        return None
    value = labels[0].find_next_sibling("p")
    return value.get_text(" ", strip=True) if value else None


def _price(value: str | None) -> int:
    match = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)\s*/1M", value or "")
    if not match:
        raise RuntimeError("lyceum: expected USD per million tokens")
    amount = Decimal(match[1]) * 1_000_000
    if amount != amount.to_integral_value():
        raise RuntimeError("lyceum: unsupported sub-microdollar price precision")
    return int(amount)


def parse_cards(html: str) -> dict[str, tuple[ModelPrice, dict[str, Any]]]:
    cards: dict[str, tuple[ModelPrice, dict[str, Any]]] = {}
    for card in BeautifulSoup(html, "html.parser").select(".model-card"):
        identity = card.select_one(".model-card-id")
        native_id = identity.get_text(strip=True) if identity else ""
        if not native_id or canonical_model_id(native_id) is None or native_id in cards:
            raise RuntimeError("lyceum: missing, invalid or duplicate model-card ID")
        context = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(K|M)", _field(card, "Context") or "")
        if context is None:
            raise RuntimeError("lyceum: missing or invalid context limit")
        limit = int(Decimal(context[1]) * (1_000 if context[2] == "K" else 1_000_000))
        if limit <= 0:
            raise RuntimeError("lyceum: nonpositive context limit")
        categories = str(card.get("data-model-categories", "")).split(",")
        embedding = "embedding" in categories
        prompt = _price(_field(card, "Input"))
        output_field = _field(card, "Output")
        completion = 0 if embedding and output_field is None else _price(output_field)
        cached_field = _field(card, "Cached")
        cached = _price(cached_field) if cached_field is not None else None
        if prompt <= 0 or (embedding and completion != 0) or (not embedding and completion <= 0):
            raise RuntimeError("lyceum: invalid billable price")
        if cached is not None and cached > prompt:
            raise RuntimeError("lyceum: cache-read price exceeds input price")
        cards[native_id] = (
            ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached),
            {
                "display_name": str(card.get("data-model-name") or native_id),
                "model_type": "embedding" if embedding else "chat",
                "context_length": limit,
                "max_output_tokens": 0 if embedding else min(65_536, limit),
                "input_modalities": ["text", "image"] if "multimodal" in categories else ["text"],
                "output_modalities": ["embeddings"] if embedding else ["text"],
                "endpoints": ["embeddings"] if embedding else ["chat/completions"],
                "supported_features": [] if embedding else ["tools", "streaming"] + (["prompt_caching"] if cached is not None else []),
            },
        )
    if not cards:
        raise RuntimeError("lyceum: no model cards; refusing empty publication")
    return cards


def discover(payload: object, html: str) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("lyceum: catalog has no data list")
    cards = parse_cards(html)
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for source in payload["data"]:
        native_id = source.get("id") if isinstance(source, dict) else None
        model_id = canonical_model_id(native_id) if isinstance(native_id, str) else None
        if model_id is None or model_id in rows:
            raise RuntimeError("lyceum: invalid or duplicate catalog ID")
        row: dict[str, Any] = {"id": model_id, "upstream_id": native_id, "display_name": native_id}
        if native_id in cards:
            price, metadata = cards[native_id]
            prices[model_id] = price
            row.update(metadata)
        else:
            # Never infer an alias/instant variant's price from its base model.
            row.update(
                routable=False,
                routable_reason="price-unavailable" if model_id in REVIEWED_UNPRICED_IDS else "awaiting-price",
                endpoints=["chat/completions"],
            )
        rows[model_id] = row
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("lyceum: no valid priced models" if not prices else "; ".join(errors))
    return prices, rows


def _probe(api_key: str, row: dict[str, Any]) -> bool:
    if row.get("model_type") != "embedding":
        return probe_openai_chat(
            base_url=BASE_URL, api_key=api_key, model=row["upstream_id"],
            max_tokens=512, expected_content="PONG", prompt="Reply with exactly PONG and nothing else.",
        )
    try:
        response = httpx.post(
            f"{BASE_URL}/embeddings", headers={"Authorization": f"Bearer {api_key}"},
            json={"model": row["upstream_id"], "input": "PONG"}, timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        vector = payload["data"][0]["embedding"]
        tokens = payload["usage"]["prompt_tokens"]
        return bool(
            isinstance(vector, list) and vector
            and all(type(value) in (int, float) and math.isfinite(value) for value in vector)
            and type(tokens) is int and tokens > 0
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return False


def _api_tariffs(source: dict[str, Any]) -> dict[str, int]:
    prices: dict[str, int] = {}
    allowed = {
        "input_modalities": {"prompt", "cached_prompt"},
        "output_modalities": {"completion"},
    }
    for field, meters in allowed.items():
        modalities = source.get(field)
        if not isinstance(modalities, list) or not modalities:
            raise RuntimeError(f"lyceum: missing {field}")
        for modality in modalities:
            if not isinstance(modality, dict) or not isinstance(modality.get("type"), str):
                raise RuntimeError("lyceum: invalid modality tariffs")
            tariffs = modality.get("pricing", [])
            if not isinstance(tariffs, list):
                raise RuntimeError("lyceum: invalid modality tariffs")
            seen: set[str] = set()
            for tariff in tariffs:
                if not isinstance(tariff, dict):
                    raise RuntimeError("lyceum: invalid tariff")
                if tariff.get("currency") != "USD":
                    continue
                meter = tariff.get("meter")
                match = next((name for name in meters if meter == f"serverless_inference_{name}_tokens"), None)
                value = tariff.get("unit_price")
                if match is None or match in seen or not isinstance(value, str) or len(value) > 64:
                    raise RuntimeError("lyceum: unknown or duplicate USD token tariff")
                seen.add(match)
                try:
                    amount = Decimal(value) * 1_000_000_000_000
                except InvalidOperation as exc:
                    raise RuntimeError("lyceum: invalid USD token price") from exc
                if not amount.is_finite() or not 0 <= amount <= 10**12 or amount != amount.to_integral_value():
                    raise RuntimeError("lyceum: invalid USD token price precision")
                if match in prices and prices[match] != amount:
                    raise RuntimeError("lyceum: modality-dependent token prices are unsupported")
                prices[match] = int(amount)
    return prices


def discover_api(payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("lyceum: catalog has no data list")
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for source in payload["data"]:
        native = source.get("id") if isinstance(source, dict) else None
        model_id = canonical_model_id(native) if isinstance(native, str) else None
        if model_id is None or model_id in rows:
            raise RuntimeError("lyceum: invalid or duplicate catalog ID")
        row: dict[str, Any] = {"id": model_id, "upstream_id": native, "display_name": native}
        rows[model_id] = row
        # Old model IDs and router/instant aliases now redirect to different
        # weights or behavior. Never silently attribute those calls to the old ID.
        if source.get("model_id") != native:
            row.update(routable=False, routable_reason="upstream-model-alias")
            continue
        if source.get("publication_status") != "published":
            row.update(routable=False, routable_reason="provider-unpublished")
            continue
        kind = source.get("kind")
        if kind not in {"chat", "embedding"}:
            row.update(routable=False, routable_reason="unsupported-endpoint")
            continue
        embedding = kind == "embedding"
        limit = source.get("context_length")
        max_output = 0 if embedding else source.get("max_output_tokens")
        if type(limit) is not int or limit <= 0 or type(max_output) is not int or not 0 <= max_output <= limit or (not embedding and max_output == 0):
            raise RuntimeError("lyceum: invalid token limits")
        tariffs = _api_tariffs(source)
        prompt = tariffs.get("prompt", 0)
        completion = tariffs.get("completion", 0)
        cached = tariffs.get("cached_prompt")
        if prompt <= 0 or (embedding and completion != 0) or (not embedding and completion <= 0):
            raise RuntimeError("lyceum: missing or nonpositive USD token tariff")
        if cached is not None and (cached > prompt or (embedding and cached != 0)):
            raise RuntimeError("lyceum: unsupported cache price")
        if embedding:
            cached = None
        inputs = {m.get("type") for m in source["input_modalities"]}
        outputs = {m.get("type") for m in source["output_modalities"]}
        if "text" not in inputs or outputs != ({"embedding"} if embedding else {"text"}):
            raise RuntimeError("lyceum: unsupported modalities")
        prices[model_id] = ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached)
        row.update(
            display_name=source.get("display_name") or native,
            model_type=kind, context_length=limit, max_output_tokens=max_output,
            input_modalities=["text", "image"] if "image" in inputs and not embedding else ["text"],
            output_modalities=["embeddings"] if embedding else ["text"],
            endpoints=["embeddings"] if embedding else ["chat/completions"],
            supported_features=[] if embedding else ["tools", "streaming"] + (["prompt_caching"] if cached is not None else []),
        )
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("lyceum: no valid priced models" if not prices else "; ".join(errors))
    return prices, rows


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    _DISCOVERED_ROWS = {}
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("LYCEUM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("lyceum: LYCEUM_API_KEY is required for discovery")
    prices, rows = discover_api(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}))
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    checked = sorted(models_requiring_canary(MANIFEST_PATH, set(prices)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda model_id: _probe(key, rows[model_id]), checked))
    healthy = {model_id for model_id, passed in zip(checked, outcomes, strict=True) if passed}
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    _DISCOVERED_ROWS = rows
    return ProviderPricingResult(
        slug=SLUG, prices=prices, source="api", fetched_url=PRICING_URL,
        notes=[f"{len(prices)} priced models; {len(healthy)}/{len(checked)} new/unhealthy canaries passed"],
        price_index_model_ids=frozenset(model_id for model_id in prices if rows[model_id]["model_type"] == "chat"),
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("lyceum: fetch must succeed before writing manifest")
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS,
        source_url=URL, pricing_source_url=PRICING_URL,
        operator_hold_reasons={model_id: row["routable_reason"] for model_id, row in _DISCOVERED_ROWS.items() if row.get("routable") is False and row.get("routable_reason") != "provider-canary-failed"},
    )
