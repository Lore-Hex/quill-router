"""Alibaba Cloud Model Studio — provider-native model catalog.

The workspace endpoint publishes an OpenAI-compatible `/models` list but not
pricing. Prices below are Alibaba Cloud Model Studio's published international
per-million-token rates for the relevant model families. Unknown families are
intentionally skipped instead of published at a guessed or zero price.
"""

from __future__ import annotations

import os

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.model_ids import remember_upstream_id

SLUG = "alibaba"
URL = (
    os.environ.get("ALIBABA_BASE_URL")
    or "https://ws-el6e4bpnggpx7g88.eu-central-1.maas.aliyuncs.com/compatible-mode/v1"
) + "/models"

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
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
    return None


def _price(native_id: str) -> ModelPrice | None:
    model = native_id.lower()
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
    elif model.startswith("qwen3.7-plus"):
        prompt, completion, cached = 0.40, 1.60, 0.04
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


def fetch() -> ProviderPricingResult:
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
        price = _price(native_id)
        if price is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        prices[model_id] = price

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
