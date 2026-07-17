"""Parasail — public pricing-page scraper + /v1/models liveness gate.

History: this module used to be a hand-pasted price table because
Parasail's pricing lived behind a SaaS login (saas.parasail.io) and
api.parasail.io's /v1/models has no pricing block (still true —
re-verified 2026-07-13). Parasail has since published a public
pricing page at https://www.parasail.io/pricing (Astro-rendered,
server-side HTML), so prices now parse from the "Per-token model
pricing" table there, hourly, like every other scraped provider.

Two-source rule (unchanged from the hand-table era): a model is
priced only if it appears on BOTH the pricing page AND /v1/models.
Page-only rows (e.g. Nemotron 3 Ultra as of 2026-07-13) and
API-only models (e.g. qwen3.5-9b) surface as notes so an operator
notices, but never produce a price.

The pricing page's row structure this parser depends on:

    <div class="ptbl-row" ...>
      <div class="mdl" ...><span class="ep" ...>DISPLAY NAME</span></div>
      <div ...><span class="num" ...>$IN</span></div>
      <div ...><span class="num" ...>$OUT</span></div>
      <div ...><span class="num" ...>$CACHED</span></div>
    </div>

scoped to the section between "Per-token model pricing" and
"Reserved GPU pricing" (the later "Self-service batch pricing"
tables reuse the same row markup for per-param-size tiers and must
not be parsed as models).
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id

SLUG = "parasail"
URL = "https://api.parasail.io/v1/models"
PRICING_URL = "https://www.parasail.io/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "parasail.json"
)

# Models we expect on BOTH the pricing page and /v1/models. Drift
# detector only — a miss lands in notes, it does not fail the run.
# 2026-07-13 sweep: kimi-k2.5, glm-4.7, deepseek-v3.2 and
# step-3.5-flash disappeared from the public pricing page (k2.5 and
# glm-4.7 from /v1/models too) — removed here; mappings kept below
# so they light back up if Parasail restores them.
EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
    "google/gemma-4-26b-a4b-it",
    "google/gemma-3-27b-it",
    "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/llama-4-maverick",
    "qwen/qwen2.5-vl-72b-instruct",
    "qwen/qwen3-vl-235b-a22b-instruct",
    "qwen/qwen3-vl-8b-instruct",
    "qwen/qwen3-235b-a22b-2507",
    "qwen/qwen3-coder-next",
    "qwen/qwen3.5-397b-a17b",
    "qwen/qwen3.5-35b-a3b",
    "qwen/qwen3.6-35b-a3b",
    "qwen/qwen3-next-80b-a3b-instruct",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5",
    "z-ai/glm-5.1",
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7-code",
    "minimax/minimax-m2.5",
    "minimax/minimax-m3",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "mistralai/mistral-small-3.2-24b-instruct",
    "thedrummer/cydonia-24b-v4.1",
    "thedrummer/skyfall-36b-v2",
    "arcee-ai/trinity-large-thinking",
    "bytedance/ui-tars-1.5-7b",
]


# Parasail-native id → OR-canonical id. The /v1/models endpoint
# returns BOTH forms for every model: a `parasail-X` slug AND the
# upstream-author form (e.g. both `parasail-gemma-4-31b-it` and
# `google/gemma-4-31B-it`), occasionally with case-variant dupes
# (`MiniMaxAI/MiniMax-M3` and `MiniMaxAI/Minimax-M3`). We map every
# observed alias to the same OR-canonical entry so route lookup works
# whichever alias is on the wire. Last swept against the live API
# 2026-07-13.
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
    "parasail-glm-52": "z-ai/glm-5.2",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "zai-org/GLM-5.2-FP8": "z-ai/glm-5.2",
    "parasail-glm47": "z-ai/glm-4.7",
    "zai-org/GLM-4.7": "z-ai/glm-4.7",
    "zai-org/GLM-4.7-FP8": "z-ai/glm-4.7",
    # kimi / moonshot
    "parasail-kimi-k25": "moonshotai/kimi-k2.5",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "moonshotai/kimi-k2.5": "moonshotai/kimi-k2.5",
    "parasail-kimi-k26": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "parasail-kimi-k27-code": "moonshotai/kimi-k2.7-code",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    # minimax
    "parasail-minimax-m25": "minimax/minimax-m2.5",
    "MiniMaxAI/MiniMax-M2.5": "minimax/minimax-m2.5",
    "parasail-minimax-m3": "minimax/minimax-m3",
    "MiniMaxAI/MiniMax-M3": "minimax/minimax-m3",
    "MiniMaxAI/Minimax-M3": "minimax/minimax-m3",
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
UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}

# SKIPPED — not yet supported by TR's chat-completions path:
#   - parasail-bge-m3 / BAAI/bge-m3 (embedding model)
#   - parasail-qwen3-embedding-4b / -8b (embedding models)
#   - parasail-resemble-tts-en (text-to-speech, priced per MChar)
# SKIPPED — Parasail-only / not in OR snapshot:
#   - parasail-mimo-v25 / XiaomiMiMo/MiMo-V2.5 (catalog has only the
#     -pro variants via xiaomi; base v2.5 is a different checkpoint)
#   - parasail-qwen35-9b (present in OR snapshot but page doesn't
#     price it yet — lights up automatically when the page adds it)
#   - "Skyfall 31B v4.2" page row (OR has the 36b-v2 sibling only)
# Revisit once OR adds them or operator wants parasail-only TR entries.


# Pricing-page display name → OR-canonical id. The page is the price
# SOURCE; /v1/models is the liveness gate. A display name here that
# stops appearing on the page just drops out of pricing (both-rule);
# a page row NOT in this map lands in notes as "unmapped" so the
# operator adds it deliberately.
_DISPLAY_TO_OR_ID = {
    "GLM-5.2": "z-ai/glm-5.2",
    "GLM-5.1": "z-ai/glm-5.1",
    "GLM-5": "z-ai/glm-5",
    "GLM-4.7": "z-ai/glm-4.7",
    "MiniMax M3": "minimax/minimax-m3",
    "MiniMax M2.5": "minimax/minimax-m2.5",
    "DeepSeek V4 Pro": "deepseek/deepseek-v4-pro",
    "DeepSeek V4 Flash": "deepseek/deepseek-v4-flash",
    "DeepSeek V3.2": "deepseek/deepseek-v3.2",
    "Kimi K2.7 Code": "moonshotai/kimi-k2.7-code",
    "Kimi K2.6": "moonshotai/kimi-k2.6",
    "Kimi K2.5": "moonshotai/kimi-k2.5",
    "Qwen3.6 35B-A3B": "qwen/qwen3.6-35b-a3b",
    "Qwen3.5 397B-A17B": "qwen/qwen3.5-397b-a17b",
    "Qwen3.5 35B-A3B": "qwen/qwen3.5-35b-a3b",
    "Qwen3-Coder-Next": "qwen/qwen3-coder-next",
    "Qwen3-VL 235B-A22B": "qwen/qwen3-vl-235b-a22b-instruct",
    "Qwen3-VL 8B": "qwen/qwen3-vl-8b-instruct",
    "Qwen3-Next 80B": "qwen/qwen3-next-80b-a3b-instruct",
    "Qwen3 235B-A22B (2507)": "qwen/qwen3-235b-a22b-2507",
    "Qwen2.5-VL 72B": "qwen/qwen2.5-vl-72b-instruct",
    "Mistral Small 3.2 24B": "mistralai/mistral-small-3.2-24b-instruct",
    "Llama 4 Maverick (FP8)": "meta-llama/llama-4-maverick",
    "Llama 3.3 70B (FP8)": "meta-llama/llama-3.3-70b-instruct",
    "Nemotron 3 Ultra 550B (NVFP4)": "nvidia/nemotron-3-ultra-550b-a55b",
    "Trinity Large (Thinking)": "arcee-ai/trinity-large-thinking",
    "Gemma 4 26B-A4B": "google/gemma-4-26b-a4b-it",
    "Gemma 4 31B": "google/gemma-4-31b-it",
    "Gemma 3 27B": "google/gemma-3-27b-it",
    "Skyfall 36B v2 (FP8)": "thedrummer/skyfall-36b-v2",
    "Cydonia 24B v4.1": "thedrummer/cydonia-24b-v4.1",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "UI-TARS 1.5 7B": "bytedance/ui-tars-1.5-7b",
    "Step 3.5 Flash": "stepfun/step-3.5-flash",
}

# Page rows we deliberately do not price (kept out of "unmapped" noise).
_DISPLAY_SKIP = {
    "Resemble TTS (English)",  # TTS, priced per MChar not MTok
    "BGE-M3",  # embedding model, single price column
    "gpt-oss-120b (Fast)",  # speed-tier variant of the same catalog entry
    "Skyfall 31B v4.2",  # not in OR catalog (36b-v2 sibling only)
    "MiMo v2.5",  # catalog carries only the -pro variants (xiaomi direct)
}

_SECTION_START = "Per-token model pricing"
_SECTION_END = "Reserved GPU pricing"

_ROW_RE = re.compile(
    r'<div class="ptbl-row"[^>]*>\s*'
    r'<div class="mdl"[^>]*><span class="ep"[^>]*>([^<]+)</span></div>'
    r"(.*?)</div>\s*</div>",
    re.S,
)
_NUM_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")


def _parse_pricing_page(html: str) -> tuple[dict[str, tuple[float, float, float | None]], list[str]]:
    """Parse the per-token table into display-name → ($/M in, $/M out,
    $/M cached | None). Returns (rows, notes)."""
    notes: list[str] = []
    start = html.find(_SECTION_START)
    if start < 0:
        raise ValueError(f"pricing page missing section marker {_SECTION_START!r}")
    end = html.find(_SECTION_END, start)
    section = html[start : end if end > start else len(html)]

    rows: dict[str, tuple[float, float, float | None]] = {}
    for match in _ROW_RE.finditer(section):
        display = match.group(1).strip()
        nums = [float(n) for n in _NUM_RE.findall(match.group(2))]
        if display in _DISPLAY_SKIP:
            continue
        if len(nums) < 2:
            notes.append(f"page row {display!r} has {len(nums)} price column(s) — skipped")
            continue
        cached = nums[2] if len(nums) >= 3 else None
        rows[display] = (nums[0], nums[1], cached)
    if not rows:
        raise ValueError("pricing page parsed to zero model rows — layout changed?")
    return rows, notes


def _model_price_from_usd_per_m(
    prompt: float, completion: float, cached: float | None
) -> ModelPrice:
    """Convert per-MTok dollar values to a ModelPrice in micro/M.
    $1.00 per MTok = 1_000_000 micro per MTok."""
    return ModelPrice(
        prompt_micro_per_m=int(round(prompt * 1_000_000)),
        completion_micro_per_m=int(round(completion * 1_000_000)),
        prompt_cached_micro_per_m=(
            int(round(cached * 1_000_000)) if cached is not None else None
        ),
    )


def _http_client() -> httpx.Client:
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    return httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    )


def fetch() -> ProviderPricingResult:
    """Scrape prices from the public pricing page, gate on /v1/models
    liveness, and return prices only for models that appear in BOTH."""
    notes: list[str] = []

    # Price source: the public pricing page. A fetch/parse failure
    # raises so the refresh pipeline falls back to the last-known
    # snapshot prices instead of publishing an empty provider.
    with _http_client() as client:
        page = client.get(PRICING_URL, headers={"User-Agent": PROVIDER_FETCH_UA})
        page.raise_for_status()
    display_rows, parse_notes = _parse_pricing_page(page.text)
    notes.extend(parse_notes)

    page_prices: dict[str, tuple[float, float, float | None]] = {}
    for display, rates in display_rows.items():
        or_id = _DISPLAY_TO_OR_ID.get(display)
        if or_id is None:
            notes.append(
                f"page row {display!r} not in _DISPLAY_TO_OR_ID — map it to enable"
            )
            continue
        page_prices[or_id] = rates

    # Liveness gate: /v1/models (auth optional). On failure, treat all
    # known natives as live — same degraded behavior as the hand-table era.
    api_key = os.environ.get("PARASAIL_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    live_native: set[str] = set()
    try:
        with _http_client() as client:
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

    or_ids_live: set[str] = set()
    for native_id in live_native:
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        or_ids_live.add(or_id)

    prices: dict[str, ModelPrice] = {}
    for or_id, rates in sorted(page_prices.items()):
        if or_id not in or_ids_live:
            notes.append(f"page prices {or_id} but /v1/models doesn't list it — skipped")
            continue
        prices[or_id] = _model_price_from_usd_per_m(*rates)

    unpriced = sorted(or_ids_live - set(page_prices.keys()))
    if unpriced:
        notes.append(
            f"Parasail serves {len(unpriced)} mapped model(s) the pricing page "
            f"doesn't list: {', '.join(unpriced)}"
        )

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=PRICING_URL,
        notes=notes,
    )


# Rows for models Parasail serves AHEAD of the shared OR snapshot.
# The manifest makes them routable at runtime before OpenRouter's
# feed catches up; prices are injected hourly from the scrape. Keys
# absent from `fetch()` prices simply keep their previous values.
# Metadata (context/modalities) mirrors the OR snapshot entries for
# the same checkpoints served by other providers.
_MANIFEST_ROW_TEMPLATES: dict[str, dict[str, Any]] = {
    "z-ai/glm-5.2": {
        "id": "z-ai/glm-5.2",
        "upstream_id": "parasail-glm-52",
        "display_name": "Parasail GLM 5.2",
        "title": "z-ai/glm-5.2",
        "context_length": 262144,
        "max_output_tokens": 262144,
        "model_type": "chat",
        "features": ["reasoning", "function-calling", "structured-outputs", "serverless"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
    },
    "qwen/qwen3.5-397b-a17b": {
        "id": "qwen/qwen3.5-397b-a17b",
        "upstream_id": "parasail-qwen35-397b-a17b",
        "display_name": "Parasail Qwen3.5 397B A17B",
        "title": "qwen/qwen3.5-397b-a17b",
        "context_length": 262144,
        "max_output_tokens": 65536,
        "model_type": "chat",
        "features": ["reasoning", "function-calling", "serverless"],
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
    },
    "moonshotai/kimi-k2.7-code": {
        "id": "moonshotai/kimi-k2.7-code",
        "upstream_id": "parasail-kimi-k27-code",
        "display_name": "Parasail Kimi K2.7 Code",
        "title": "moonshotai/kimi-k2.7-code",
        "context_length": 262144,
        "max_output_tokens": 65536,
        "model_type": "chat",
        "features": ["function-calling", "structured-outputs", "serverless"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
    },
    "minimax/minimax-m3": {
        "id": "minimax/minimax-m3",
        "upstream_id": "parasail-minimax-m3",
        "display_name": "Parasail MiniMax M3",
        "title": "minimax/minimax-m3",
        "context_length": 1048576,
        "max_output_tokens": 65536,
        "model_type": "chat",
        "features": ["reasoning", "function-calling", "serverless"],
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
    },
}

# Manifest rows that must always end up priced after a refresh.
_MANIFEST_EXPECTED = [
    "z-ai/glm-5.2",
    "qwen/qwen3.5-397b-a17b",
    "moonshotai/kimi-k2.7-code",
    "minimax/minimax-m3",
]


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Refresh `provider_models/parasail.json` from scraped prices.

    Mirrors the wafer.py hook: update existing supplement rows in
    place, append templates for newly-served ahead-of-snapshot models,
    and stamp provenance. Rows are NOT removed when a model temporarily
    drops off the page — the runtime treats stale supplement prices as
    better than delisting a routable model mid-day; removal is an
    operator decision."""
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("parasail manifest has no models list")

    existing_by_id: dict[str, dict[str, Any]] = {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    updated: list[str] = []
    appended: list[str] = []
    for model_id, price in sorted(result.prices.items()):
        row = existing_by_id.get(model_id)
        if row is None:
            template = _MANIFEST_ROW_TEMPLATES.get(model_id)
            if template is None:
                continue
            row = dict(template)
            rows.append(row)
            existing_by_id[model_id] = row
            appended.append(model_id)
        tier = price.tiers[0]
        row["input_token_price_per_m"] = tier.prompt_micro_per_m
        row["output_token_price_per_m"] = tier.completion_micro_per_m
        if tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        else:
            row.pop("cached_input_token_price_per_m", None)
        updated.append(model_id)

    missing = sorted(set(_MANIFEST_EXPECTED) - set(updated))
    if missing:
        raise RuntimeError(f"parasail manifest did not update expected model(s): {missing}")

    raw["_about"] = (
        "Provider-native supplement for Parasail models live ahead of the "
        "shared snapshot. Prices refreshed hourly from the public pricing "
        "page (www.parasail.io/pricing); model liveness gated on "
        "api.parasail.io/v1/models."
    )
    raw["source"] = PRICING_URL
    raw["generated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    suffix = f", appended {len(appended)}" if appended else ""
    return [f"parasail: refreshed provider_models/parasail.json ({len(updated)} priced rows{suffix})"]
