"""Alibaba Cloud Model Studio — provider-native model catalog.

The workspace endpoint publishes an OpenAI-compatible `/models` list but not
pricing. Prices below are Alibaba Cloud Model Studio's published international
per-million-token rates for the relevant model families. Unknown families are
intentionally skipped instead of published at a guessed or zero price.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    PriceTier,
    ProviderPricingResult,
    guard_manifest_prune,
    reconcile_manifest_tombstones,
    validate,
)
from scripts.pricing.model_ids import remember_upstream_id

SLUG = "alibaba"
URL = (
    os.environ.get("ALIBABA_BASE_URL")
    or "https://ws-el6e4bpnggpx7g88.eu-central-1.maas.aliyuncs.com/compatible-mode/v1"
) + "/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "alibaba.json"
)

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "qwen/qwen3.7-flash",
    "qwen/qwen3.7-max",
    "qwen/qwen3.7-plus",
]


def _micro(dollars_per_million: float) -> int:
    return int(round(dollars_per_million * 1_000_000))


def _canonical_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith("glm-"):
        return f"z-ai/{lowered}"
    if lowered.startswith("kimi-"):
        return f"moonshotai/{lowered}"
    if lowered.startswith("deepseek-"):
        return f"deepseek/{lowered}"
    if lowered.startswith("qwen") or lowered.startswith("qwq"):
        return f"qwen/{lowered}"
    if lowered.startswith("minimax-"):
        return f"minimax/{lowered}"
    # Keep every model returned by Alibaba's authenticated OpenAI-compatible
    # catalog visible to the refresh system. Unknown families are namespaced
    # to Alibaba and enter the manifest as non-routable ``awaiting-price``
    # rows until a reviewed price rule exists; they must never be silently
    # ignored or accidentally exposed as zero-cost routes.
    if "/" in lowered:
        return lowered
    return f"alibaba/{lowered}"


def _price(native_id: str) -> ModelPrice | None:
    model = native_id.lower()
    if model.startswith("qwen3.7-flash"):
        return ModelPrice(
            tiers=[
                PriceTier(
                    max_prompt_tokens=32_000,
                    prompt_micro_per_m=_micro(0.03),
                    completion_micro_per_m=_micro(0.13),
                    prompt_cached_micro_per_m=_micro(0.006),
                ),
                PriceTier(
                    max_prompt_tokens=256_000,
                    prompt_micro_per_m=_micro(0.10),
                    completion_micro_per_m=_micro(0.40),
                    prompt_cached_micro_per_m=_micro(0.02),
                ),
                PriceTier(
                    max_prompt_tokens=None,
                    prompt_micro_per_m=_micro(0.20),
                    completion_micro_per_m=_micro(0.80),
                    prompt_cached_micro_per_m=_micro(0.04),
                ),
            ]
        )
    if model.startswith("qwen3.7-plus"):
        return ModelPrice(
            tiers=[
                PriceTier(
                    max_prompt_tokens=256_000,
                    prompt_micro_per_m=_micro(0.40),
                    completion_micro_per_m=_micro(1.60),
                    prompt_cached_micro_per_m=_micro(0.04),
                ),
                PriceTier(
                    max_prompt_tokens=None,
                    prompt_micro_per_m=_micro(1.20),
                    completion_micro_per_m=_micro(4.80),
                    prompt_cached_micro_per_m=_micro(0.12),
                ),
            ]
        )
    prompt = completion = cached = None
    if model.startswith("glm-5."):
        prompt, completion, cached = 1.40, 4.40, 0.26
    elif model == "kimi-k2.7-code":
        prompt, completion, cached = 0.894, 3.713, 0.18
    elif model.startswith("kimi-k2."):
        prompt, completion, cached = 0.574, 3.011, 0.115
    elif model == "deepseek-v4-flash":
        prompt, completion, cached = 0.20, 0.40, 0.02
    elif model == "deepseek-v4-pro":
        prompt, completion, cached = 2.40, 4.80, 0.24
    elif model.startswith("qwen3.7-max"):
        prompt, completion, cached = 2.50, 7.50, 0.25
    elif model.startswith("qwen3.6-plus"):
        prompt, completion, cached = 0.50, 3.00, 0.05
    elif model.startswith("qwen3.6-flash"):
        prompt, completion, cached = 0.25, 1.50, 0.025
    elif model == "qwen3.6-35b-a3b":
        prompt, completion, cached = 0.375, 2.25, 0.0375
    elif model == "qwen3.6-27b":
        prompt, completion, cached = 0.60, 3.60, 0.06
    elif model.startswith("qwen3.5-flash"):
        prompt, completion, cached = 0.10, 0.40, 0.01
    elif model == "qwen3.5-397b-a17b":
        prompt, completion, cached = 0.60, 3.60, 0.06
    elif model == "qwen3.5-122b-a10b":
        prompt, completion, cached = 0.40, 3.20, 0.04
    elif model == "qwen3.5-35b-a3b":
        prompt, completion, cached = 0.25, 2.00, 0.025
    elif model == "qwen3.5-27b":
        prompt, completion, cached = 0.30, 2.40, 0.03
    elif model.startswith("qwen3.5-plus"):
        prompt, completion, cached = 0.40, 2.40, 0.04
    elif model.startswith("qwen3-vl-plus"):
        prompt, completion, cached = 0.20, 1.60, 0.02
    elif model.startswith("qwen3-vl-flash") or model.startswith("qwen3-vl-8b"):
        prompt, completion, cached = 0.05, 0.40, 0.005
    elif model.startswith("qwen-vl-ocr"):
        prompt, completion, cached = 0.07, 0.16, 0.007
    elif model.startswith("qwen3-coder-plus"):
        prompt, completion, cached = 1.00, 5.00, 0.10
    elif model.startswith("qwen3-coder-flash") or model == "qwen3-coder-next":
        prompt, completion, cached = 0.30, 1.50, 0.03
    elif model == "qwen3-coder-480b-a35b-instruct":
        prompt, completion, cached = 1.50, 7.50, 0.15
    elif model == "qwen3-coder-30b-a3b-instruct":
        prompt, completion, cached = 0.45, 2.25, 0.045
    elif model.startswith("qwen3-next-80b-a3b"):
        prompt, completion, cached = 0.15, 1.20, 0.015
    elif model == "qwen3-235b-a22b-thinking-2507":
        prompt, completion, cached = 0.23, 2.30, 0.023
    elif model == "qwen3-235b-a22b-instruct-2507":
        prompt, completion, cached = 0.23, 0.92, 0.023
    elif model == "qwen3-30b-a3b-thinking-2507":
        prompt, completion, cached = 0.20, 2.40, 0.02
    elif model == "qwen3-30b-a3b-instruct-2507":
        prompt, completion, cached = 0.20, 0.80, 0.02
    elif model == "qwen3-235b-a22b" or model.startswith("qwen3-vl-235b"):
        prompt, completion, cached = 0.70, 8.40, 0.07
    elif model == "qwen3-32b":
        prompt, completion, cached = 0.16, 0.64, 0.016
    elif model == "qwen3-30b-a3b" or model.startswith("qwen3-vl-30b") or model.startswith("qwen3-vl-32b"):
        prompt, completion, cached = 0.20, 2.40, 0.02
    elif model == "qwen3-14b":
        prompt, completion, cached = 0.35, 4.20, 0.035
    elif model == "qwen3-8b":
        prompt, completion, cached = 0.18, 2.10, 0.018
    elif model.startswith("qwen3-max"):
        prompt, completion, cached = 1.20, 6.00, 0.12
    elif model.startswith("qwen-plus"):
        prompt, completion, cached = 0.40, 4.00, 0.04
    elif model.startswith("qwen-flash") or model.startswith("qwen-mt-"):
        prompt, completion, cached = 0.05, 0.40, 0.005
    if prompt is None or completion is None:
        return None
    return ModelPrice(
        prompt_micro_per_m=_micro(prompt),
        completion_micro_per_m=_micro(completion),
        prompt_cached_micro_per_m=_micro(cached or 0),
    )


UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _manifest_row(
    *,
    model_id: str,
    native_id: str,
    source_row: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": model_id,
        "upstream_id": native_id,
        "display_name": native_id.replace("-", " ").title(),
        "title": native_id,
        "model_type": "chat",
        "endpoints": ["chat/completions"],
    }
    created = source_row.get("created")
    if isinstance(created, int) and not isinstance(created, bool) and created > 0:
        row["created"] = created
    if native_id.startswith("qwen3.7-"):
        row["context_length"] = 1_048_576
    if native_id.startswith("qwen3.7-flash"):
        row["input_modalities"] = ["text", "image"]
        row["output_modalities"] = ["text"]
    return row


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = (
        os.environ.get("ALIBABA_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or os.environ.get("ALIYUN_API_KEY")
    )
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []

    prices: dict[str, ModelPrice] = {}
    manifest_rows: dict[str, dict[str, Any]] = {}
    UPSTREAM_ID_MAP.clear()
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        model_id = _canonical_model_id(native_id)
        if model_id is None:
            continue
        manifest_rows[model_id] = _manifest_row(
            model_id=model_id,
            native_id=native_id,
            source_row=row,
        )
        price = _price(native_id)
        if price is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        prices[model_id] = price

    _DISCOVERED_MANIFEST_ROWS = manifest_rows
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=["Alibaba /models does not include prices; family rates come from published Model Studio pricing."],
    )


def _apply_price(row: dict[str, Any], price: ModelPrice) -> None:
    first = price.tiers[0]
    row["input_token_price_per_m"] = first.prompt_micro_per_m
    row["output_token_price_per_m"] = first.completion_micro_per_m
    if first.prompt_cached_micro_per_m is not None:
        row["cached_input_token_price_per_m"] = first.prompt_cached_micro_per_m
    else:
        row.pop("cached_input_token_price_per_m", None)

    if len(price.tiers) == 1:
        # Several older Alibaba families have hand-verified context tiers in
        # the committed manifest while the public source parser currently
        # exposes only the headline rate. Do not erase those safer tiers.
        return
    row["price_tiers"] = [
        {
            "max_prompt_tokens": tier.max_prompt_tokens,
            "input_token_price_per_m": tier.prompt_micro_per_m,
            "output_token_price_per_m": tier.completion_micro_per_m,
            **(
                {"cached_input_token_price_per_m": tier.prompt_cached_micro_per_m}
                if tier.prompt_cached_micro_per_m is not None
                else {}
            ),
        }
        for tier in price.tiers
    ]


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Refresh Alibaba routes from the workspace's authoritative model feed."""

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("alibaba manifest has no models list")
    if not _DISCOVERED_MANIFEST_ROWS:
        guarded = guard_manifest_prune(rows, [], provider_slug=SLUG)
        if guarded is rows:
            return ["alibaba: kept old manifest (mass-prune guard)"]
        raise RuntimeError("alibaba discovery returned no supported model rows")

    existing_by_id = {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    present_rows: dict[str, dict[str, Any]] = {}
    updated: list[str] = []
    for model_id, discovered in sorted(_DISCOVERED_MANIFEST_ROWS.items()):
        existing = existing_by_id.get(model_id)
        if existing is None:
            row = dict(discovered)
        else:
            # The live /models feed carries only id/object/created. Preserve
            # richer curated metadata while refreshing route identity and
            # availability from the authoritative feed.
            row = dict(existing)
            row["id"] = discovered["id"]
            row["upstream_id"] = discovered["upstream_id"]
            if "created" in discovered:
                row["created"] = discovered["created"]
        price = result.prices.get(model_id)
        if price is not None:
            _apply_price(row, price)
            updated.append(model_id)
        present_rows[model_id] = row

    refreshed_rows = reconcile_manifest_tombstones(
        rows,
        present_rows,
        priced_ids=set(result.prices),
        source=result.source,
    )
    guarded = guard_manifest_prune(rows, refreshed_rows, provider_slug=SLUG)
    if guarded is rows:
        return ["alibaba: kept old manifest (mass-prune guard)"]

    previous_ids = set(existing_by_id)
    current_ids = {
        row["id"]
        for row in guarded
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    appended = sorted(current_ids - previous_ids)
    raw["models"] = guarded
    raw["source"] = URL
    raw["price_source"] = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(guarded)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    suffix = f", appended {len(appended)}" if appended else ""
    return [
        f"alibaba: refreshed provider_models/alibaba.json "
        f"({len(updated)} priced rows{suffix})"
    ]
