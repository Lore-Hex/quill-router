"""Nscale authenticated mixed-modality catalog and first-party prices."""

from __future__ import annotations

import base64
import binascii
import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    emit_workflow_warning,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    guard_fixed_output_prices,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import positive_int, probe_openai_chat

SLUG = "nscale"
BASE_URL = "https://inference.api.nscale.com/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://docs.nscale.com/docs/ai-services/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/nscale.json"
)
MANIFEST_STALE_FALLBACK = True

EMBEDDING_UPSTREAM_ID = "Qwen/Qwen3-Embedding-8B"
IMAGE_UPSTREAM_ID = "black-forest-labs/FLUX.1-schnell"
EMBEDDING_MODEL_ID = "qwen/qwen3-embedding-8b"
IMAGE_MODEL_ID = "black-forest-labs/flux.1-schnell"
IMAGE_SIZE = "1024x1024"
IMAGE_DIMENSIONS = (1024, 1024)
IMAGE_PIXELS = 1024 * 1024
MAX_IMAGE_RESPONSE_BYTES = 16 * 1024 * 1024

UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def _catalog_rows(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("nscale: model catalog has no data list")
    return [row for row in payload["data"] if isinstance(row, dict)]


def _microdollars_per_million(value: object) -> int | None:
    """Convert Nscale's dollars-per-million value without float arithmetic."""

    try:
        dollars = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not dollars.is_finite() or dollars < 0:
        return None
    return int((dollars * Decimal(1_000_000)).to_integral_value(ROUND_HALF_UP))


def _image_price_microdollars(value: object) -> int | None:
    """Convert Nscale's dollars-per-megapixel rate for its 1024px square."""

    try:
        dollars_per_megapixel = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not dollars_per_megapixel.is_finite() or dollars_per_megapixel <= 0:
        return None
    # Nscale's authenticated catalog declares USD per decimal megapixel. The
    # gateway accepts only 1024x1024 for this route, so reserve the exact pixel
    # area and round cost upward by less than one microdollar.
    price = dollars_per_megapixel * Decimal(IMAGE_PIXELS) / Decimal(1_000_000)
    return int((price * Decimal(1_000_000)).to_integral_value(ROUND_CEILING))


def _pricing(row: dict[str, Any]) -> tuple[int, int] | None:
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = _microdollars_per_million(pricing.get("input"))
    completion = _microdollars_per_million(pricing.get("output"))
    if prompt is None or completion is None:
        return None
    return prompt, completion


def _canonical_id(native_id: str) -> str | None:
    if native_id == EMBEDDING_UPSTREAM_ID:
        return EMBEDDING_MODEL_ID
    if native_id == IMAGE_UPSTREAM_ID:
        return IMAGE_MODEL_ID
    return mapped_or_canonical_model_id(native_id, {})


def _discover(
    rows: list[dict[str, Any]],
) -> tuple[
    dict[str, ModelPrice],
    dict[str, ModelPrice],
    dict[str, dict[str, Any]],
]:
    """Return chat-index prices, all manifest prices, and manifest rows."""

    chat_prices: dict[str, ModelPrice] = {}
    manifest_prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in rows:
        native_id = source.get("id")
        if not isinstance(native_id, str):
            continue
        model_id = _canonical_id(native_id)
        if model_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        context_length = positive_int(source.get("context_length"))

        if native_id == IMAGE_UPSTREAM_ID:
            row: dict[str, Any] = {
                "id": model_id,
                "upstream_id": native_id,
                "display_name": "FLUX.1 Schnell",
                "model_type": "image",
                "input_modalities": ["text"],
                "output_modalities": ["image"],
                "endpoints": ["images"],
                "supported_features": ["image-generation"],
                "status": 1,
            }
            pricing = source.get("pricing")
            prompt = (
                _microdollars_per_million(pricing.get("input"))
                if isinstance(pricing, dict)
                else None
            )
            fixed_price = (
                _image_price_microdollars(pricing.get("output"))
                if isinstance(pricing, dict)
                else None
            )
            if prompt != 0 or fixed_price is None:
                row["routable"] = False
                row["routable_reason"] = "price-unavailable"
                discovered[model_id] = row
                continue
            row["fixed_output_price_microdollars"] = {"1k": fixed_price}
            manifest_prices[model_id] = ModelPrice(0, 0)
        elif native_id == EMBEDDING_UPSTREAM_ID:
            row = {
                "id": model_id,
                "upstream_id": native_id,
                "display_name": "Qwen3 Embedding 8B",
                "model_type": "embedding",
                "input_modalities": ["text"],
                "output_modalities": ["embeddings"],
                "endpoints": ["embeddings"],
                "status": 1,
            }
            if context_length is not None:
                row["context_length"] = context_length
            raw_price = _pricing(source)
            if raw_price is None or raw_price[0] <= 0 or raw_price[1] != 0:
                row["routable"] = False
                row["routable_reason"] = "price-unavailable"
                discovered[model_id] = row
                continue
            prompt, _completion = raw_price
            manifest_prices[model_id] = ModelPrice(prompt, 0)
        else:
            raw_price = _pricing(source)
            if raw_price is None:
                continue
            prompt, completion = raw_price
            if prompt <= 0 or completion <= 0:
                continue
            row = {
                "id": model_id,
                "upstream_id": native_id,
                "display_name": native_id.split("/", 1)[-1],
                "model_type": "chat",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "endpoints": ["chat/completions"],
                "status": 1,
            }
            if context_length is not None:
                row["context_length"] = context_length
            price = ModelPrice(prompt, completion)
            chat_prices[model_id] = price
            manifest_prices[model_id] = price
        discovered[model_id] = row

    errors = validate(chat_prices, [])
    if errors:
        raise RuntimeError("; ".join(errors))
    if len(chat_prices) < 15:
        raise RuntimeError("nscale: fewer than 15 priced chat models discovered")
    return chat_prices, manifest_prices, discovered


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": PROVIDER_FETCH_UA,
    }


def _probe_embedding(api_key: str) -> bool:
    try:
        response = httpx.post(
            f"{BASE_URL}/embeddings",
            headers=_headers(api_key),
            json={"model": EMBEDDING_UPSTREAM_ID, "input": "PONG"},
            timeout=PROVIDER_FETCH_TIMEOUT,
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return bool(
        isinstance(data, list)
        and len(data) == 1
        and isinstance(data[0], dict)
        and isinstance(data[0].get("embedding"), list)
        and data[0]["embedding"]
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in data[0]["embedding"]
        )
        and isinstance(usage, dict)
        and isinstance(usage.get("prompt_tokens"), int)
        and not isinstance(usage["prompt_tokens"], bool)
        and usage["prompt_tokens"] > 0
    )


def _probe_image(api_key: str) -> bool:
    try:
        response = httpx.post(
            f"{BASE_URL}/images/generations",
            headers=_headers(api_key),
            json={
                "model": IMAGE_UPSTREAM_ID,
                "prompt": "A single black square on a white background",
                "n": 1,
                "size": IMAGE_SIZE,
            },
            timeout=120,
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
        data = payload["data"]
        if not isinstance(data, list) or len(data) != 1:
            return False
        encoded = data[0]["b64_json"]
        if not isinstance(encoded, str):
            return False
        raw = base64.b64decode(encoded, validate=True)
        if not 64 < len(raw) <= MAX_IMAGE_RESPONSE_BYTES:
            return False
    except (KeyError, IndexError, TypeError, ValueError, binascii.Error):
        return False
    try:
        with Image.open(BytesIO(raw)) as image:
            dimensions = image.size
            image_format = image.format
            image.verify()
    # Pillow uses several exception classes for corrupt codecs. A malformed
    # provider response is a failed canary, never a reason to abort every
    # provider's price refresh.
    except Exception:  # noqa: BLE001
        return False
    return dimensions == IMAGE_DIMENSIONS and image_format in {"JPEG", "PNG", "WEBP"}


def _probe(api_key: str, model_id: str) -> bool:
    if model_id == EMBEDDING_MODEL_ID:
        return _probe_embedding(api_key)
    if model_id == IMAGE_MODEL_ID:
        return _probe_image(api_key)
    upstream_id = UPSTREAM_ID_MAP.get(model_id)
    if upstream_id is None:
        return False
    return probe_openai_chat(
        base_url=BASE_URL,
        api_key=api_key,
        model=upstream_id,
        max_tokens=256,
        expected_content="PONG",
    )


def _quarantine_fixed_price_changes(discovered: dict[str, dict[str, Any]]) -> int:
    """Disable media whose provider price no longer matches the enclave."""

    if not MANIFEST_PATH.exists():
        return 0
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("nscale: existing manifest is unreadable") from exc
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("nscale: existing manifest has no models list")
    existing = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    quarantined = 0
    for model_id, row in discovered.items():
        old = existing.get(model_id)
        if old is None:
            continue
        old_price = old.get("fixed_output_price_microdollars")
        new_price = row.get("fixed_output_price_microdollars")
        if not isinstance(old_price, dict) or not isinstance(new_price, dict):
            continue
        if old_price == new_price:
            continue
        row["observed_fixed_output_price_microdollars"] = new_price
        row["fixed_output_price_microdollars"] = old_price
        row["routable"] = False
        row["routable_reason"] = "fixed-price-change-pending-enclave"
        quarantined += 1
    if quarantined:
        emit_workflow_warning(
            f"nscale: disabled {quarantined} media route(s) until enclave pricing is updated"
        )
    return quarantined


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    api_key = os.environ.get("NSCALE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("nscale: NSCALE_API_KEY is required for discovery")
    UPSTREAM_ID_MAP.clear()
    _DISCOVERED_ROWS = {}
    rows = _catalog_rows(fetch_json(URL, extra_headers={"Authorization": f"Bearer {api_key}"}))
    chat_prices, manifest_prices, discovered = _discover(rows)
    canary_candidates = {
        model_id for model_id, row in discovered.items() if row.get("routable") is not False
    }
    checked = tuple(models_requiring_canary(MANIFEST_PATH, canary_candidates))
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = dict(
            zip(
                checked,
                pool.map(lambda model_id: _probe(api_key, model_id), checked),
                strict=True,
            )
        )
    healthy = {model_id for model_id, passed in outcomes.items() if passed}
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )
    quarantined_price_changes = _quarantine_fixed_price_changes(discovered)
    guard_fixed_output_prices(MANIFEST_PATH, discovered)
    _DISCOVERED_ROWS = discovered
    embedding_count = sum(row.get("model_type") == "embedding" for row in discovered.values())
    image_count = sum(row.get("model_type") == "image" for row in discovered.values())
    return ProviderPricingResult(
        slug=SLUG,
        prices=manifest_prices,
        source="api",
        fetched_url=URL,
        notes=[
            (
                f"discovered {len(chat_prices)} chat, {embedding_count} embedding, "
                f"and {image_count} image model"
            ),
            f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed",
            f"quarantined {quarantined_price_changes} fixed-price changes",
        ],
        price_index_model_ids=frozenset(chat_prices),
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("nscale: fetch must succeed before writing manifest")
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=URL,
        pricing_source_url=PRICING_URL,
    )
