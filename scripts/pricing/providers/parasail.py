"""Parasail — human-only provider config.

Parasail's dashboard pricing lives behind a SaaS login
(saas.parasail.io/info/pricing) that the Jina-markdown scraper
can't see, and api.parasail.io's /v1/models endpoint doesn't
include a pricing block. Static `_PRICES` table below is
operator-pasted from the dashboard.

Format: OR-canonical-id → (prompt_micro_per_m, completion_micro_per_m,
prompt_cached_micro_per_m). Every row carries a trailing comment
with the date the row was pasted, the operator initials, and (when
visible) the per-MTok dollar values exactly as they appeared in
the dashboard. When you add/change a row, keep that audit trail —
the next person to look will need to know what to distrust.

When Parasail publishes a machine-readable price feed, swap this
scraper to the API-direct pattern used by lightning.py / gmi.py /
deepinfra.py.
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

SLUG = "parasail"
URL = "https://api.parasail.io/v1/models"

EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
    "google/gemma-4-26b-a4b-it",
    "google/gemma-3-27b-it",
    "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/llama-4-maverick",
    "qwen/qwen2.5-vl-72b-instruct",
    "deepseek/deepseek-v3.2",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-4.7",
    "z-ai/glm-5",
    "z-ai/glm-5.1",
    "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2.6",
    "minimax/minimax-m2.5",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-235b-a22b-2507",
    "qwen/qwen3-coder-next",
    "qwen/qwen3-vl-235b-a22b-instruct",
    "qwen/qwen3-vl-8b-instruct",
    "qwen/qwen3.5-397b-a17b",
    "qwen/qwen3.5-35b-a3b",
    "qwen/qwen3.6-35b-a3b",
    "qwen/qwen3-next-80b-a3b-instruct",
    "mistralai/mistral-small-3.2-24b-instruct",
    "thedrummer/cydonia-24b-v4.1",
    "thedrummer/skyfall-36b-v2",
    "stepfun/step-3.5-flash",
    "arcee-ai/trinity-large-thinking",
    "bytedance/ui-tars-1.5-7b",
]


# Parasail-native id → OR-canonical id. The /v1/models endpoint
# returns BOTH forms for every model: a `parasail-X` slug AND the
# upstream-author form (e.g. both `parasail-gemma-4-31b-it` and
# `google/gemma-4-31B-it`). We map both to the same OR-canonical
# entry so route lookup works whichever alias is on the wire.
# Source: live probe of https://api.parasail.io/v1/models on
# 2026-05-12 cross-checked against the saas.parasail.io dashboard
# pricing page the operator pasted.
_NATIVE_TO_OR_ID = {
    # gemma-4 family
    "parasail-gemma-4-31b-it": "google/gemma-4-31b-it",
    "google/gemma-4-31B-it": "google/gemma-4-31b-it",
    "google/gemma-4-31b-it": "google/gemma-4-31b-it",
    "google/gemma-4-31B": "google/gemma-4-31b-it",
    "parasail-gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it",
    "google/gemma-4-26B-A4B-it": "google/gemma-4-26b-a4b-it",
    "google/gemma-4-26B-A4B": "google/gemma-4-26b-a4b-it",
    # gemma-3
    "parasail-gemma3-27b-it": "google/gemma-3-27b-it",
    "google/gemma-3-27b-it": "google/gemma-3-27b-it",
    # llama
    "parasail-llama-33-70b-fp8": "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "parasail-llama-4-maverick-instruct-fp8": "meta-llama/llama-4-maverick",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct": "meta-llama/llama-4-maverick",
    # qwen vision / instruct
    "parasail-qwen25-vl-72b-instruct": "qwen/qwen2.5-vl-72b-instruct",
    "Qwen/Qwen2.5-VL-72B-Instruct": "qwen/qwen2.5-vl-72b-instruct",
    "parasail-qwen3-vl-235b-a22b-instruct": "qwen/qwen3-vl-235b-a22b-instruct",
    "Qwen/Qwen3-VL-235B-A22B-Instruct": "qwen/qwen3-vl-235b-a22b-instruct",
    "parasail-qwen3vl-8b-instruct": "qwen/qwen3-vl-8b-instruct",
    "Qwen/Qwen3-VL-8B-Instruct": "qwen/qwen3-vl-8b-instruct",
    "parasail-qwen3-235b-a22b-instruct-2507": "qwen/qwen3-235b-a22b-2507",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-2507",
    "parasail-qwen3-coder-next": "qwen/qwen3-coder-next",
    "Qwen/Qwen3-Coder-Next": "qwen/qwen3-coder-next",
    "parasail-qwen35-397b-a17b": "qwen/qwen3.5-397b-a17b",
    "Qwen/Qwen3.5-397B-A17B": "qwen/qwen3.5-397b-a17b",
    "parasail-qwen3p5-35b-a3b": "qwen/qwen3.5-35b-a3b",
    "Qwen/Qwen3.5-35B-A3B": "qwen/qwen3.5-35b-a3b",
    "Qwen/Qwen3.5-35B-A3B-FP8": "qwen/qwen3.5-35b-a3b",
    "parasail-qwen3p6-35b-a3b": "qwen/qwen3.6-35b-a3b",
    "Qwen/Qwen3.6-35B-A3B": "qwen/qwen3.6-35b-a3b",
    "parasail-qwen-3-next-80b-instruct": "qwen/qwen3-next-80b-a3b-instruct",
    "Qwen/Qwen3-Next-80B-A3B-Instruct": "qwen/qwen3-next-80b-a3b-instruct",
    # deepseek
    "parasail-deepseek-v32": "deepseek/deepseek-v3.2",
    "deepseek-ai/DeepSeek-V3.2": "deepseek/deepseek-v3.2",
    "parasail-deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-ai/DeepSeek-V4-Flash": "deepseek/deepseek-v4-flash",
    "parasail-deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    # z-ai / GLM
    "parasail-glm-5": "z-ai/glm-5",
    "zai-org/GLM-5": "z-ai/glm-5",
    "zai-org/GLM-5-FP8": "z-ai/glm-5",
    "parasail-glm-51": "z-ai/glm-5.1",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "zai-org/GLM-5.1-FP8": "z-ai/glm-5.1",
    "parasail-glm47": "z-ai/glm-4.7",
    "zai-org/GLM-4.7": "z-ai/glm-4.7",
    "zai-org/GLM-4.7-FP8": "z-ai/glm-4.7",
    # kimi / moonshot
    "parasail-kimi-k25": "moonshotai/kimi-k2.5",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2.5": "moonshotai/kimi-k2.5",
    "parasail-kimi-k26": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    # minimax
    "parasail-minimax-m25": "minimax/minimax-m2.5",
    "MiniMaxAI/MiniMax-M2.5": "minimax/minimax-m2.5",
    # gpt-oss
    "parasail-gpt-oss-120b": "openai/gpt-oss-120b",
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "parasail-gpt-oss-20b": "openai/gpt-oss-20b",
    "openai/gpt-oss-20b": "openai/gpt-oss-20b",
    # mistral
    "parasail-mistral-small-32-24b": "mistralai/mistral-small-3.2-24b-instruct",
    "mistralai/Mistral-Small-3.2-24B-Instruct-2506": "mistralai/mistral-small-3.2-24b-instruct",
    # thedrummer (cydonia, skyfall) — these are real models on OR
    "parasail-cydonia-24-v41": "thedrummer/cydonia-24b-v4.1",
    "TheDrummer/Cydonia-24B-v4.1": "thedrummer/cydonia-24b-v4.1",
    "parasail-skyfall-36b-v2-fp8": "thedrummer/skyfall-36b-v2",
    "TheDrummer/Skyfall-36B-v2": "thedrummer/skyfall-36b-v2",
    # stepfun
    "parasail-stepfun35-flash": "stepfun/step-3.5-flash",
    "stepfun-ai/Step-3.5-Flash-FP8": "stepfun/step-3.5-flash",
    # arcee trinity
    "parasail-trinity-large-thinking": "arcee-ai/trinity-large-thinking",
    "arcee-ai/Trinity-Large-Thinking": "arcee-ai/trinity-large-thinking",
    "arcee-ai/Trinity-Large-Thinking-FP8-Block": "arcee-ai/trinity-large-thinking",
    # bytedance ui-tars
    "parasail-ui-tars-1p5-7b": "bytedance/ui-tars-1.5-7b",
    "ByteDance-Seed/UI-TARS-1.5-7B": "bytedance/ui-tars-1.5-7b",
}

# SKIPPED — not yet supported by TR's chat-completions path:
#   - parasail-bge-m3 / BAAI/bge-m3 (embedding model)
#   - parasail-resemble-tts-en (text-to-speech, priced per MChar)
# SKIPPED — Parasail-only / not in OR snapshot:
#   - parasail-skyfall-31b-v42 (OR has the 36b-v2 sibling but not 31b-v42)
#   - parasail-nemotron-3-nano-30b-a3b-fp8
#   - parasail-nemotron-3-super-120b-a12b-fp8
# Revisit once OR adds them or operator wants parasail-only TR catalog entries.


# Operator-pasted rates from saas.parasail.io/info/pricing
# (cross-checked 2026-05-12 against live api.parasail.io/v1/models).
# Format: OR-canonical-id → (prompt_$/M, completion_$/M, cached_input_$/M | None).
# Every row carries an audit comment with the dashboard's displayed
# per-MTok dollar values exactly as they appeared, so a future
# operator can spot drift on the next paste.
_RATES_USD_PER_M: dict[str, tuple[float, float, float | None]] = {
    # gemma family — jp 2026-05-11 / 2026-05-12 dashboard
    "google/gemma-4-31b-it": (0.14, 0.40, 0.10),
    "google/gemma-4-26b-a4b-it": (0.13, 0.40, 0.05),
    "google/gemma-3-27b-it": (0.08, 0.45, 0.04),
    # llama — jp 2026-05-12
    "meta-llama/llama-3.3-70b-instruct": (0.22, 0.50, 0.11),
    "meta-llama/llama-4-maverick": (0.35, 1.00, 0.17),
    # qwen
    "qwen/qwen2.5-vl-72b-instruct": (0.80, 1.00, 0.40),
    "qwen/qwen3-vl-235b-a22b-instruct": (0.21, 1.90, 0.10),
    "qwen/qwen3-vl-8b-instruct": (0.25, 0.75, 0.12),
    "qwen/qwen3-235b-a22b-2507": (0.10, 0.60, 0.05),
    "qwen/qwen3-coder-next": (0.12, 0.80, 0.07),
    "qwen/qwen3.5-397b-a17b": (0.50, 3.60, 0.30),
    "qwen/qwen3.5-35b-a3b": (0.15, 1.00, 0.05),
    "qwen/qwen3.6-35b-a3b": (0.15, 1.00, 0.05),
    "qwen/qwen3-next-80b-a3b-instruct": (0.10, 1.10, 0.07),
    # deepseek
    "deepseek/deepseek-v3.2": (0.28, 0.45, 0.13),
    "deepseek/deepseek-v4-flash": (0.14, 0.28, 0.07),
    "deepseek/deepseek-v4-pro": (1.74, 3.48, 0.87),
    # z-ai
    "z-ai/glm-5": (1.00, 3.20, 0.20),
    "z-ai/glm-5.1": (1.40, 4.40, 0.26),
    "z-ai/glm-4.7": (0.45, 2.10, 0.11),
    # moonshot
    "moonshotai/kimi-k2.5": (0.60, 2.80, 0.20),
    "moonshotai/kimi-k2.6": (0.80, 3.50, 0.20),
    # minimax
    "minimax/minimax-m2.5": (0.30, 1.20, 0.03),
    # gpt-oss
    "openai/gpt-oss-120b": (0.10, 0.75, 0.055),
    "openai/gpt-oss-20b": (0.04, 0.20, 0.02),
    # mistral
    "mistralai/mistral-small-3.2-24b-instruct": (0.09, 0.60, 0.05),
    # thedrummer / arcee / stepfun / bytedance
    "thedrummer/cydonia-24b-v4.1": (0.30, 0.50, 0.15),
    "thedrummer/skyfall-36b-v2": (0.55, 0.80, 0.25),
    "stepfun/step-3.5-flash": (0.10, 0.30, 0.03),
    "arcee-ai/trinity-large-thinking": (0.22, 0.85, 0.06),
    "bytedance/ui-tars-1.5-7b": (0.10, 0.20, 0.10),
}


def _model_price_from_usd_per_m(
    prompt: float, completion: float, cached: float | None
) -> ModelPrice:
    """Convert per-MTok dollar values to a ModelPrice in micro/M.
    $1 = 1_000_000 micro = $1.00 per MTok = 1_000_000 micro per MTok."""
    return ModelPrice(
        prompt_micro_per_m=int(round(prompt * 1_000_000)),
        completion_micro_per_m=int(round(completion * 1_000_000)),
        prompt_cached_micro_per_m=(
            int(round(cached * 1_000_000)) if cached is not None else None
        ),
    )


def fetch() -> ProviderPricingResult:
    """Hit /v1/models for liveness, then look up each served
    OR-canonical id in `_RATES_USD_PER_M`. Returns prices only for
    models that appear in BOTH (Parasail serving it AND operator
    has pasted a rate)."""
    api_key = os.environ.get("PARASAIL_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    notes: list[str] = []
    live_native: set[str] = set()
    try:
        transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
        with httpx.Client(
            timeout=PROVIDER_FETCH_TIMEOUT,
            follow_redirects=True,
            transport=transport,
        ) as client:
            response = client.get(URL, headers=headers)
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("data") or []
        live_native = {
            str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")
        }
    except Exception as exc:  # noqa: BLE001
        notes.append(f"/v1/models fetch failed ({exc}); treating all known natives as live")
        live_native = set(_NATIVE_TO_OR_ID.keys())

    or_ids_live = {_NATIVE_TO_OR_ID[n] for n in live_native if n in _NATIVE_TO_OR_ID}
    prices: dict[str, ModelPrice] = {}
    for or_id, rates in _RATES_USD_PER_M.items():
        if or_id not in or_ids_live:
            notes.append(f"have a price for {or_id} but /v1/models doesn't list it — skipped")
            continue
        prices[or_id] = _model_price_from_usd_per_m(*rates)

    # Surface models Parasail serves that we don't yet have rates
    # for so the operator notices and pastes them.
    unpriced = sorted(or_ids_live - set(_RATES_USD_PER_M.keys()))
    if unpriced:
        notes.append(
            f"Parasail serves {len(unpriced)} mapped model(s) without rates in "
            f"_RATES_USD_PER_M: {', '.join(unpriced)} — paste from dashboard to enable"
        )

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )
