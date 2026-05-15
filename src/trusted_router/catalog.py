from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict

from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    TOKENS_PER_MILLION,
    dollars_to_microdollars,
    microdollars_per_million_tokens_to_token_decimal,
)


@dataclass(frozen=True)
class Provider:
    slug: str
    name: str
    supports_chat: bool = True
    supports_messages: bool = False
    supports_embeddings: bool = False
    supports_prepaid: bool = False
    supports_byok: bool = True
    attested_gateway: bool = True
    stores_content: bool = False


@dataclass(frozen=True)
class PriceTier:
    """One tier of context-conditional pricing. A request whose prompt
    token count is ≤ `max_prompt_tokens` uses this tier's rates. The
    LAST tier in `Model.price_tiers` MUST have `max_prompt_tokens=None`
    (uncapped fallback). Most models have exactly one tier.

    Both prompt and completion rates live on the tier — Gemini-Pro-shape
    pricing flips both rates when context crosses 200k tokens.

    `prompt_cached_*` is the discounted rate for prompt tokens that
    upstream reports as cache hits. None ⇒ upstream charges the same
    rate cached or not (rare; most providers offer a cache discount).
    Per-token billing splits the prompt into (uncached × full rate) +
    (cached × cached rate); see `cost_microdollars` in routes/helpers.
    """

    max_prompt_tokens: int | None
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    prompt_cached_price_microdollars_per_million_tokens: int | None = None


def _flat_tier(
    prompt: int,
    completion: int,
    prompt_cached: int | None = None,
) -> tuple[PriceTier, ...]:
    """Construct a length-1 tier tuple (the common case)."""
    return (
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=prompt,
            completion_price_microdollars_per_million_tokens=completion,
            prompt_cached_price_microdollars_per_million_tokens=prompt_cached,
        ),
    )


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    provider: str
    context_length: int
    upstream_id: str | None = None
    supports_chat: bool = True
    supports_messages: bool = False
    supports_embeddings: bool = False
    prepaid_available: bool = False
    byok_available: bool = True
    # Headline (low-tier) rates: what /v1/models displays. For
    # tier-aware billing, use `price_tiers` instead and pick the right
    # tier based on the actual prompt token count.
    prompt_price_microdollars_per_million_tokens: int = 0
    completion_price_microdollars_per_million_tokens: int = 0
    published_prompt_price_microdollars_per_million_tokens: int = 0
    published_completion_price_microdollars_per_million_tokens: int = 0
    # Full tier list for context-conditional pricing. Defaults to a
    # single tier matching the headline rates above; the ingest path
    # populates multi-tier values when the snapshot carries them.
    price_tiers: tuple[PriceTier, ...] = ()
    published_price_tiers: tuple[PriceTier, ...] = ()


@dataclass(frozen=True)
class ModelEndpoint:
    id: str
    model_id: str
    provider: str
    usage_type: str
    upstream_id: str | None = None
    prompt_price_microdollars_per_million_tokens: int = 0
    completion_price_microdollars_per_million_tokens: int = 0
    published_prompt_price_microdollars_per_million_tokens: int = 0
    published_completion_price_microdollars_per_million_tokens: int = 0
    price_tiers: tuple[PriceTier, ...] = ()
    published_price_tiers: tuple[PriceTier, ...] = ()

    @property
    def is_byok(self) -> bool:
        return self.usage_type.lower() == "byok"


def select_price_tier(tiers: tuple[PriceTier, ...], prompt_tokens: int) -> PriceTier:
    """Pick the tier that applies to a request with `prompt_tokens` of
    input. Walks the tiers in order; returns the first one whose
    threshold accommodates the prompt size. The last tier always has
    max_prompt_tokens=None and is the catch-all.

    Used by the billing path to compute actual cost. For models with
    a single uncapped tier (the common case), this returns that tier
    regardless of `prompt_tokens`.
    """
    for tier in tiers:
        if tier.max_prompt_tokens is None or prompt_tokens <= tier.max_prompt_tokens:
            return tier
    # Should be unreachable — the last tier always matches due to
    # max_prompt_tokens=None — but defend against malformed catalog data.
    return tiers[-1]


class ModelPricingKwargs(TypedDict):
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    published_prompt_price_microdollars_per_million_tokens: int
    published_completion_price_microdollars_per_million_tokens: int


# Uniform pricing: customer pays cost + 10%, floor $0.01/M tokens. Same
# value goes into both `prompt_price_*` and `published_*` — TR no longer
# runs the 1¢/M "discount theater". The floor catches free upstream tiers
# so the catalog never advertises $0/M to end users; $0.01/M is ~10×
# margin over real per-request infra cost (~$0.00001/req on a typical
# 10K-token call), recovered via 10K-tokens × $0.01/M = $0.0001/req.
_PRICE_MARKUP_RATIO = Decimal("1.10")
_PRICE_FLOOR_MICRODOLLARS_PER_M = 10_000  # $0.01 per million tokens.


def _customer_price(cost_microdollars_per_million: int) -> int:
    """Apply the markup formula. Input/output in microdollars per million tokens."""
    marked_up = int(
        (Decimal(cost_microdollars_per_million) * _PRICE_MARKUP_RATIO).to_integral_value()
    )
    return max(marked_up, _PRICE_FLOOR_MICRODOLLARS_PER_M)


def _priced(cost_dollars_per_million: str | int | float) -> tuple[int, int, int]:
    """Return (prompt_price, published_price, cost_microdollars) for a
    dollars-per-million cost. prompt_price == published_price under the
    uniform formula; cost is preserved as a third value for any consumer
    that wants the upstream-paid amount (e.g. the per-endpoint detail page)."""
    cost = dollars_to_microdollars(cost_dollars_per_million)
    customer = _customer_price(cost)
    return customer, customer, cost


def _customer_price_from_dollars_per_token(price_per_token: str) -> tuple[int, int, int]:
    """Variant for snapshot-shaped inputs (dollars/token strings).
    Returns the same triple as `_priced`."""
    if not price_per_token:
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    try:
        per_token = Decimal(str(price_per_token))
    except (InvalidOperation, ValueError):
        # Malformed snapshot rows are pinned to the price floor — better
        # to advertise $0.01/M than to crash module import or expose $0.
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    cost = int((per_token * MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION).to_integral_value())
    customer = _customer_price(cost)
    return customer, customer, cost


def _read_pricing_tiers(pricing: dict[str, Any], dimension: str) -> tuple[PriceTier, ...] | None:
    """Read `pricing.prompt_tiers` / `pricing.completion_tiers` arrays
    from the snapshot. Returns None if the snapshot has only flat
    pricing for this model — caller should construct a single-tier
    list from the headline rate in that case.

    Tier shape in the snapshot:
        prompt_tiers:     [{"max_prompt_tokens": int|None,
                            "prompt": "$/tok",
                            "input_cache_read": "$/tok"  # optional}]
        completion_tiers: [{"max_prompt_tokens": int|None, "completion": "$/tok"}]

    Both arrays have the same length and same `max_prompt_tokens`
    sequence. Returned PriceTier objects pair them up; cached prompt
    rate is parsed from `input_cache_read` (matches OR's convention).
    """
    raw_prompt = pricing.get("prompt_tiers")
    raw_completion = pricing.get("completion_tiers")
    if not isinstance(raw_prompt, list) or not isinstance(raw_completion, list):
        return None
    if not raw_prompt or len(raw_prompt) != len(raw_completion):
        return None
    tiers: list[PriceTier] = []
    for prompt_tier, completion_tier in zip(raw_prompt, raw_completion, strict=False):
        if not isinstance(prompt_tier, dict) or not isinstance(completion_tier, dict):
            return None
        threshold = prompt_tier.get("max_prompt_tokens")
        if threshold is not None and not isinstance(threshold, int):
            return None
        prompt_per_token = str(prompt_tier.get("prompt") or "")
        completion_per_token = str(completion_tier.get("completion") or "")
        prompt_micro, _pub, _cost = _customer_price_from_dollars_per_token(prompt_per_token)
        completion_micro, _pub2, _cost2 = _customer_price_from_dollars_per_token(
            completion_per_token
        )
        cached_micro: int | None = None
        cache_read = prompt_tier.get("input_cache_read")
        if cache_read:
            cached_micro, _pub3, _cost3 = _customer_price_from_dollars_per_token(str(cache_read))
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_price_microdollars_per_million_tokens=prompt_micro,
                completion_price_microdollars_per_million_tokens=completion_micro,
                prompt_cached_price_microdollars_per_million_tokens=cached_micro,
            )
        )
    if tiers[-1].max_prompt_tokens is not None:
        # Snapshot data is malformed — last tier should be uncapped.
        # Return None so caller falls back to the headline rate.
        return None
    return tuple(tiers)


PROVIDERS: dict[str, Provider] = {
    "trustedrouter": Provider(
        slug="trustedrouter",
        name="TrustedRouter",
        supports_messages=True,
        supports_embeddings=False,
        supports_prepaid=True,
        supports_byok=True,
    ),
    "anthropic": Provider(
        slug="anthropic", name="Anthropic", supports_messages=True, supports_prepaid=True
    ),
    "openai": Provider(
        slug="openai", name="OpenAI", supports_embeddings=True, supports_prepaid=True
    ),
    "gemini": Provider(
        slug="gemini", name="Gemini", supports_embeddings=True, supports_prepaid=True
    ),
    "cerebras": Provider(slug="cerebras", name="Cerebras", supports_prepaid=True),
    "deepseek": Provider(slug="deepseek", name="DeepSeek", supports_prepaid=True),
    "mistral": Provider(slug="mistral", name="Mistral", supports_prepaid=True),
    "kimi": Provider(slug="kimi", name="Kimi", supports_prepaid=True),
    "zai": Provider(slug="zai", name="Z.AI", supports_prepaid=True),
    # Together AI hosts a broad open-weight catalog (Llama, DeepSeek
    # incl. DeepSeek-OCR, Qwen, Mixtral) plus image gen (FLUX) and
    # embeddings — categories TR didn't otherwise cover. OpenAI-
    # compatible chat completions at api.together.xyz/v1.
    "together": Provider(
        slug="together", name="Together", supports_embeddings=True, supports_prepaid=True
    ),
    # xAI Grok — OpenAI-compatible chat completions at api.x.ai/v1.
    # As of 2026-05, headline model is grok-4.3 ($1.25/$2.50 per M).
    "grok": Provider(slug="grok", name="xAI Grok", supports_prepaid=True),
    # Novita — multi-model serverless inference. OpenAI-compatible
    # at api.novita.ai/v3/openai. Hosts DeepSeek, Qwen, Llama,
    # GLM, Kimi (and many more) at competitive rates.
    "novita": Provider(slug="novita", name="Novita AI", supports_prepaid=True),
    # Phala (RedPill) — confidential AI inference inside Intel TDX
    # / NVIDIA Confidential Compute enclaves. Verified attestation,
    # end-to-end encrypted prompts. **On-brand for TR's trust story.**
    # OpenAI-compatible at api.red-pill.ai/v1.
    "phala": Provider(slug="phala", name="Phala", supports_prepaid=True, stores_content=False),
    # SiliconFlow — Chinese serverless inference with 200+ open-weight
    # models. OpenAI-compatible at api.siliconflow.com/v1.
    "siliconflow": Provider(slug="siliconflow", name="SiliconFlow", supports_prepaid=True),
    # Tinfoil — TEE-attested confidential inference. Verified-no-logs
    # via remote attestation. **Also on-brand for TR's trust story.**
    # OpenAI-compatible at inference.tinfoil.sh/v1.
    "tinfoil": Provider(
        slug="tinfoil", name="Tinfoil", supports_prepaid=True, stores_content=False
    ),
    # Venice.AI — privacy-focused LLM gateway. No-logs, no-censoring
    # positioning. OpenAI-compatible at api.venice.ai/api/v1.
    "venice": Provider(slug="venice", name="Venice", supports_prepaid=True, stores_content=False),
    # Parasail — serverless inference platform. Hosts Llama, Qwen,
    # Gemma 4 family, plus their own quantized variants
    # (parasail-* aliases). OpenAI-compatible at api.parasail.io/v1.
    # No public pricing API — pricing scraper falls back to a static
    # table per family until they expose machine-readable rates.
    "parasail": Provider(slug="parasail", name="Parasail", supports_prepaid=True),
    # Lightning AI — Lightning's hosted inference. OpenAI-compatible at
    # lightning.ai/api/v1. Pricing is published per-model in their
    # /v1/models response (input_cost_per_token + output_cost_per_token),
    # which the scraper consumes directly without scraping HTML.
    "lightning": Provider(slug="lightning", name="Lightning AI", supports_prepaid=True),
    # GMI Cloud — confidential-GPU inference hosted on H100/H200.
    # OpenAI-compatible at api.gmi-serving.com/v1. Pricing is in the
    # /v1/models response under each model's `pricing` block (per-token
    # rates as strings).
    "gmi": Provider(slug="gmi", name="GMI Cloud", supports_prepaid=True),
    # DeepInfra — large open-weight catalog (Llama, Gemma 4, Qwen,
    # DeepSeek, etc.). OpenAI-compatible at api.deepinfra.com/v1/openai.
    # Pricing in the /v1/openai/models response under
    # metadata.pricing.{input_tokens,output_tokens} as USD per million.
    "deepinfra": Provider(slug="deepinfra", name="DeepInfra", supports_prepaid=True),
    # Nebius Token Factory — OpenAI-compatible shared inference for
    # open-weight models. The /v1/models feed publishes exact upstream
    # model IDs with mixed-case authors, so TR carries a provider-native
    # supplement and passes upstream_id through unchanged.
    "nebius": Provider(slug="nebius", name="Nebius Token Factory", supports_prepaid=True),
    # MiniMax first-party API. OpenAI-compatible at api.minimax.io/v1;
    # public TR IDs use the OpenRouter-style minimax/<slug> form while
    # endpoint.upstream_id preserves MiniMax's exact mixed-case ID.
    "minimax": Provider(slug="minimax", name="MiniMax", supports_prepaid=True),
}
# Vertex is intentionally excluded until TR's GCP project gets the
# Anthropic-on-Vertex / Gemini-on-Vertex quota approvals.

# Providers with a direct prepaid implementation in the attested
# quill-cloud-proxy llm_multi gateway. BYOK endpoints may exist for any
# keyed provider, but Credits endpoints must stay in sync with this set so
# the control plane cannot authorize a prepaid route the enclave cannot
# dispatch.
GATEWAY_PREPAID_PROVIDER_SLUGS = frozenset(
    {
        "anthropic",
        "openai",
        "gemini",
        "cerebras",
        "deepseek",
        "mistral",
        "kimi",
        "zai",
        "together",
        # New providers — all OpenAI-compatible chat completions, so
        # the existing enclave OpenAI-shape adapter can dispatch them
        # by switching base URL + auth header.
        "grok",
        "novita",
        # 2026-05-13: Phala re-enabled with the CORRECT confidential-
        # AI key. The 2026-05-12 attempt failed because we were
        # routing via the "redpill" upstream pass-through tier
        # (key 401s on chat completions even though /v1/models 200s)
        # — that key works for catalog browsing but isn't entitled
        # to chat. The fix: cloud.phala.com dashboard issues a
        # separate key for the GPU-TEE-attested confidential-AI
        # tier, stored as PHALA_CONFIDENTIAL_API_KEY → Secret
        # Manager `trustedrouter-phala-confidential-api-key`. The
        # enclave's QUILL_PHALA_SECRET default + AWS bootstrap_server
        # now point at the confidential secret; model ids ship as
        # `phala/<bare>` (per docs.phala.com/phala-cloud/confidential-ai)
        # via phalaModelMap in byok.go. Verified working live with
        # phala/gpt-oss-120b and phala/deepseek-v3.2 returning 200.
        "phala",
        "siliconflow",
        "tinfoil",
        "venice",
        # 2026-05-11 batch (all OpenAI-compatible chat completions).
        # All three host google/gemma-4 family which gives TR three
        # independent prepaid routes for the same open-weight model
        # — useful for both price arbitrage in the auto-router and
        # availability isolation when one provider is degraded.
        "parasail",
        "lightning",
        "gmi",
        "deepinfra",
        "nebius",
        "minimax",
    }
)


AUTO_MODEL_ID = "trustedrouter/auto"
FREE_MODEL_ID = "trustedrouter/free"
CHEAP_MODEL_ID = "trustedrouter/cheap"
MONITOR_MODEL_ID = "trustedrouter/monitor"
META_MODEL_IDS = frozenset({AUTO_MODEL_ID, FREE_MODEL_ID, CHEAP_MODEL_ID, MONITOR_MODEL_ID})
# IDs follow snapshot naming exactly. The picks span the 8 keyed
# providers so `trustedrouter/auto` rolls over across providers if any
# one is down. Each entry must have a provider-direct price in the
# snapshot — OR-only models can no longer reach the catalog (see
# scripts/pricing/refresh.py:_merge_snapshot).
#
# 2026-05 update: replaced openai/gpt-4o-mini with openai/gpt-5.4-mini.
# OpenAI's current pricing page only lists GPT-5.5/5.4 family + pro
# variants; the older 4o family is still served but absent from the
# canonical pricing surface, so we route auto callers to the current
# headline mid-tier model instead.
DEFAULT_AUTO_MODEL_ORDER = [
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.6",
    "openai/gpt-5.4-mini",
    "google/gemini-2.5-flash",
    "deepseek/deepseek-v4-flash",
    "moonshotai/kimi-k2.6",
    "mistralai/mistral-small-2603",
    "z-ai/glm-4.6",
]


# Catalog seed — only TR's Auto meta-model is hand-coded. Every other
# entry comes from `_INGESTED_MODELS` below, which is built from
# `data/openrouter_snapshot.json`. That guarantees pricing is uniformly
# `cost × 1.10, $0.01/M floor` (per the formula), and that the catalog
# lists every model from every provider TR has a key for — no
# hand-curated subset to drift out of sync with reality.
MODELS: dict[str, Model] = {
    AUTO_MODEL_ID: Model(
        id=AUTO_MODEL_ID,
        name="TrustedRouter Auto",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
        prompt_price_microdollars_per_million_tokens=0,
        completion_price_microdollars_per_million_tokens=0,
        published_prompt_price_microdollars_per_million_tokens=0,
        published_completion_price_microdollars_per_million_tokens=0,
    ),
    FREE_MODEL_ID: Model(
        id=FREE_MODEL_ID,
        name="TrustedRouter Free",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    CHEAP_MODEL_ID: Model(
        id=CHEAP_MODEL_ID,
        name="TrustedRouter Cheap",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    MONITOR_MODEL_ID: Model(
        id=MONITOR_MODEL_ID,
        name="TrustedRouter Monitor",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
}


def _endpoint(
    model: Model,
    *,
    usage_type: str,
    provider: str | None = None,
    upstream_id: str | None = None,
) -> ModelEndpoint:
    provider_slug = provider or model.provider
    suffix = "byok" if usage_type.lower() == "byok" else "prepaid"
    return ModelEndpoint(
        id=f"{model.id}@{provider_slug}/{suffix}",
        model_id=model.id,
        provider=provider_slug,
        usage_type="BYOK" if usage_type.lower() == "byok" else "Credits",
        upstream_id=upstream_id or model.upstream_id,
        prompt_price_microdollars_per_million_tokens=model.prompt_price_microdollars_per_million_tokens,
        completion_price_microdollars_per_million_tokens=model.completion_price_microdollars_per_million_tokens,
        published_prompt_price_microdollars_per_million_tokens=model.published_prompt_price_microdollars_per_million_tokens,
        published_completion_price_microdollars_per_million_tokens=model.published_completion_price_microdollars_per_million_tokens,
    )


def _build_endpoints(models: dict[str, Model]) -> dict[str, ModelEndpoint]:
    endpoints: dict[str, ModelEndpoint] = {}
    for model in models.values():
        if model.id in META_MODEL_IDS:
            continue
        provider = PROVIDERS[model.provider]
        if model.prepaid_available and provider.slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
            endpoint = _endpoint(model, usage_type="Credits")
            endpoints[endpoint.id] = endpoint
        if model.byok_available and provider.supports_byok:
            endpoint = _endpoint(model, usage_type="BYOK")
            endpoints[endpoint.id] = endpoint
    return endpoints


# Folder where the OpenRouter ingest snapshot lives. Bundled into the
# wheel so production reads from disk; refreshed by
# `scripts/ingest_openrouter_catalog.py` and committed via PR.
_INGEST_PATH = Path(__file__).parent / "data" / "openrouter_snapshot.json"
_PROVIDER_MODELS_DIR = Path(__file__).parent / "data" / "provider_models"

# OpenRouter publishes models as `{author}/{slug}` where author maps onto
# one of TR's keyed providers. This drops the `Model.provider` (publisher)
# field for an ingested entry.
_AUTHOR_TO_PROVIDER_SLUG: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "gemini",
    "cerebras": "cerebras",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "mistralai": "mistral",
    "moonshot": "kimi",
    "moonshotai": "kimi",
    "z-ai": "zai",
    "zhipu": "zai",
    "zhipuai": "zai",
    "x-ai": "grok",
    "xai": "grok",
    "phala": "phala",
    # `meta-llama/*`, `qwen/*`, `minimax/*` etc. fall back to whichever
    # endpoint provider serves them — Cerebras / Novita / SiliconFlow
    # all host open-weight Llama / Qwen variants, and the endpoint
    # provider determines which TR-keyed provider answers the call.
}


def _author_provider(model_id: str, endpoints: list[dict[str, Any]]) -> str | None:
    author = model_id.split("/", 1)[0].lower()
    if author in _AUTHOR_TO_PROVIDER_SLUG:
        return _AUTHOR_TO_PROVIDER_SLUG[author]
    if endpoints:
        slug = endpoints[0].get("tr_provider_slug")
        if isinstance(slug, str) and slug in PROVIDERS:
            return slug
    return None


def _ingested_models_and_endpoints() -> tuple[dict[str, Model], dict[str, ModelEndpoint]]:
    """Read the OpenRouter snapshot and return (models, endpoints) dicts.
    Pricing is run through `_customer_price_from_dollars_per_token` so the
    catalog uniformly applies the cost+10% / $0.01/M-floor formula."""
    if not _INGEST_PATH.exists():
        return {}, {}
    snapshot = json.loads(_INGEST_PATH.read_text(encoding="utf-8"))
    raw_models = snapshot.get("models")
    if not isinstance(raw_models, list):
        return {}, {}

    models: dict[str, Model] = {}
    endpoints: dict[str, ModelEndpoint] = {}

    for raw_model in raw_models:
        model_id = raw_model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        raw_endpoints = [e for e in (raw_model.get("endpoints") or []) if isinstance(e, dict)]
        if not raw_endpoints:
            continue
        publisher = _author_provider(model_id, raw_endpoints)
        if publisher is None:
            continue

        per_endpoint_prices: list[tuple[int, int, tuple[PriceTier, ...], str, dict[str, Any]]] = []
        for raw_ep in raw_endpoints:
            slug = raw_ep.get("tr_provider_slug")
            if not isinstance(slug, str) or slug not in PROVIDERS:
                continue
            pricing = raw_ep.get("pricing") or {}
            prompt_price, _, _ = _customer_price_from_dollars_per_token(
                str(pricing.get("prompt") or "0")
            )
            completion_price, _, _ = _customer_price_from_dollars_per_token(
                str(pricing.get("completion") or "0")
            )
            # Cached input rate — Anthropic / OpenAI / DeepSeek / Z.AI
            # / Kimi / Novita / Venice all expose this; OR snapshot
            # uses `input_cache_read` as the field name.
            cached_price: int | None = None
            cache_read = pricing.get("input_cache_read")
            if cache_read:
                cached_price, _, _ = _customer_price_from_dollars_per_token(str(cache_read))
            # Tier-aware pricing: read multi-tier from snapshot if present;
            # otherwise synthesize a single-tier list from the headline rate.
            tiers = _read_pricing_tiers(pricing, "prompt") or _flat_tier(
                prompt_price, completion_price, prompt_cached=cached_price
            )
            per_endpoint_prices.append((prompt_price, completion_price, tiers, slug, raw_ep))

        if not per_endpoint_prices:
            continue

        # Model-level price = cheapest endpoint headline, so /v1/models
        # top-level `pricing.prompt` doesn't lie when multiple providers
        # serve the same model at different tiers.
        cheapest_prompt = min(p for p, _c, _t, _s, _e in per_endpoint_prices)
        cheapest_completion = min(c for _p, c, _t, _s, _e in per_endpoint_prices)
        # Tier list belongs to the cheapest endpoint (matches the
        # headline rate above).
        cheapest_tiers = next(t for p, _c, t, _s, _e in per_endpoint_prices if p == cheapest_prompt)

        ctx_candidates = [
            int(raw_model.get("context_length") or 0),
            *(int(ep.get("context_length") or 0) for _p, _c, _t, _s, ep in per_endpoint_prices),
        ]
        context_length = max(ctx_candidates) or 0

        # Anthropic-native `/v1/messages` is only available for models
        # Anthropic actually serves; for everything else, /v1/messages is
        # not supported even if Claude-on-OpenRouter etc. exist. Drive
        # the supports_messages flag off the publisher.
        supports_messages = publisher == "anthropic"
        prepaid_available = any(
            slug in GATEWAY_PREPAID_PROVIDER_SLUGS for _p, _c, _t, slug, _ep in per_endpoint_prices
        )
        models[model_id] = Model(
            id=model_id,
            name=str(raw_model.get("name") or model_id),
            provider=publisher,
            context_length=context_length,
            supports_chat=True,
            supports_messages=supports_messages,
            prepaid_available=prepaid_available,
            byok_available=PROVIDERS[publisher].supports_byok,
            prompt_price_microdollars_per_million_tokens=cheapest_prompt,
            completion_price_microdollars_per_million_tokens=cheapest_completion,
            published_prompt_price_microdollars_per_million_tokens=cheapest_prompt,
            published_completion_price_microdollars_per_million_tokens=cheapest_completion,
            price_tiers=cheapest_tiers,
            published_price_tiers=cheapest_tiers,
        )

        for prompt_price, completion_price, tiers, slug, raw_ep in per_endpoint_prices:
            upstream_id = str(raw_ep.get("model_id") or model_id)
            if slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
                credits_id = f"{model_id}@{slug}/prepaid"
                endpoints[credits_id] = ModelEndpoint(
                    id=credits_id,
                    model_id=model_id,
                    provider=slug,
                    usage_type="Credits",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )
            if PROVIDERS[slug].supports_byok:
                byok_id = f"{model_id}@{slug}/byok"
                endpoints[byok_id] = ModelEndpoint(
                    id=byok_id,
                    model_id=model_id,
                    provider=slug,
                    usage_type="BYOK",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )

    return models, endpoints


def _as_positive_int(value: object) -> int:
    if not isinstance(value, int | str | float | bytes | bytearray):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _supplemental_provider_models_and_endpoints() -> tuple[
    dict[str, Model], dict[str, ModelEndpoint]
]:
    """Read provider-native model manifests for providers whose live API
    lists more routes than OpenRouter's endpoint feed. These manifests
    preserve exact upstream model IDs and provider-direct prices, so the
    control plane can authorize routes the attested gateway can actually
    call and bill.

    Novita, Nebius, and MiniMax currently use this path because their
    live `/models` feeds expose working provider-direct routes before
    OpenRouter's public endpoint catalog catches up.
    """
    models: dict[str, Model] = {}
    endpoints: dict[str, ModelEndpoint] = {}
    for provider_slug in ("novita", "nebius", "minimax"):
        path = _PROVIDER_MODELS_DIR / f"{provider_slug}.json"
        if not path.exists() or provider_slug not in PROVIDERS:
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw_models = raw.get("models")
        if not isinstance(raw_models, list):
            continue
        provider = PROVIDERS[provider_slug]
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            model_id = raw_model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            upstream_id = raw_model.get("upstream_id")
            if not isinstance(upstream_id, str) or not upstream_id:
                upstream_id = model_id
            if raw_model.get("model_type") not in (None, "chat"):
                continue
            if "chat/completions" not in {str(item) for item in (raw_model.get("endpoints") or [])}:
                continue

            prompt_cost = _as_positive_int(raw_model.get("input_token_price_per_m"))
            completion_cost = _as_positive_int(raw_model.get("output_token_price_per_m"))
            prompt_price = _customer_price(prompt_cost)
            completion_price = _customer_price(completion_cost)
            tiers = _flat_tier(prompt_price, completion_price)
            publisher = (
                _author_provider(model_id, [{"tr_provider_slug": provider_slug}]) or provider_slug
            )
            context_length = _as_positive_int(raw_model.get("context_length"))
            name = str(raw_model.get("display_name") or raw_model.get("title") or model_id)

            models[model_id] = Model(
                id=model_id,
                name=name,
                provider=publisher,
                context_length=context_length,
                upstream_id=upstream_id,
                supports_chat=True,
                supports_messages=False,
                # Availability comes from the explicit provider-native
                # endpoints below. Do not let _build_endpoints synthesize
                # publisher-direct routes for supplemental-only models
                # such as deepseek/deepseek-ocr-2@deepseek.
                prepaid_available=False,
                byok_available=False,
                prompt_price_microdollars_per_million_tokens=prompt_price,
                completion_price_microdollars_per_million_tokens=completion_price,
                published_prompt_price_microdollars_per_million_tokens=prompt_price,
                published_completion_price_microdollars_per_million_tokens=completion_price,
                price_tiers=tiers,
                published_price_tiers=tiers,
            )

            if provider_slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
                credits_id = f"{model_id}@{provider_slug}/prepaid"
                endpoints[credits_id] = ModelEndpoint(
                    id=credits_id,
                    model_id=model_id,
                    provider=provider_slug,
                    usage_type="Credits",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )
            if provider.supports_byok:
                byok_id = f"{model_id}@{provider_slug}/byok"
                endpoints[byok_id] = ModelEndpoint(
                    id=byok_id,
                    model_id=model_id,
                    provider=provider_slug,
                    usage_type="BYOK",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )
    return models, endpoints


_INGESTED_MODELS, _INGESTED_ENDPOINTS = _ingested_models_and_endpoints()
_SUPPLEMENTAL_MODELS, _SUPPLEMENTAL_ENDPOINTS = _supplemental_provider_models_and_endpoints()
# The OpenRouter ingest snapshot is the primary catalog. Provider-native
# supplements add exact routes from providers whose live model API is
# ahead of OpenRouter's endpoint feed. Pricing across both paths goes
# through the same `cost × 1.10, $0.01/M floor` formula.
MODELS.update(_INGESTED_MODELS)
for _model_id, _model in _SUPPLEMENTAL_MODELS.items():
    MODELS.setdefault(_model_id, _model)

MODEL_ENDPOINTS: dict[str, ModelEndpoint] = _build_endpoints(MODELS)
MODEL_ENDPOINTS.update(_INGESTED_ENDPOINTS)
MODEL_ENDPOINTS.update(_SUPPLEMENTAL_ENDPOINTS)


def endpoints_for_model(model_id: str) -> list[ModelEndpoint]:
    return [endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.model_id == model_id]


def endpoint_for_id(endpoint_id: str | None) -> ModelEndpoint | None:
    if endpoint_id is None:
        return None
    return MODEL_ENDPOINTS.get(endpoint_id)


def default_endpoint_for_model(model: Model) -> ModelEndpoint | None:
    endpoints = endpoints_for_model(model.id)
    if not endpoints:
        return None
    for endpoint in endpoints:
        if endpoint.usage_type == "Credits":
            return endpoint
    return endpoints[0]


def auto_candidate_models(order: str | None = None) -> list[Model]:
    raw_ids = [
        item.strip()
        for item in (order.split(",") if order else DEFAULT_AUTO_MODEL_ORDER)
        if item.strip()
    ]
    candidates: list[Model] = []
    seen: set[str] = set()
    for model_id in raw_ids:
        if model_id == AUTO_MODEL_ID or model_id in seen:
            continue
        model = MODELS.get(model_id)
        if model is not None and model.supports_chat:
            candidates.append(model)
            seen.add(model_id)
    return candidates


def free_candidate_models(limit: int = 16) -> list[Model]:
    candidates = [
        model
        for model in MODELS.values()
        if _is_regular_chat_model(model) and model.id.endswith(":free")
    ]
    candidates.sort(key=_price_sort_key)
    return candidates[:limit]


def cheap_candidate_models(limit: int = 8) -> list[Model]:
    by_provider: dict[str, Model] = {}
    for model in MODELS.values():
        if not _is_regular_chat_model(model) or model.id.endswith(":free"):
            continue
        current = by_provider.get(model.provider)
        if current is None or _price_sort_key(model) < _price_sort_key(current):
            by_provider[model.provider] = model
    return sorted(by_provider.values(), key=_price_sort_key)[:limit]


def monitor_candidate_models(limit: int = 8) -> list[Model]:
    preferred_ids = [
        "anthropic/claude-haiku-4.5",
        "z-ai/glm-4.5-air",
        "z-ai/glm-4.6",
        "moonshotai/kimi-k2.6",
        "google/gemini-2.5-flash",
        "mistralai/mistral-small-2603",
        "deepseek/deepseek-v4-flash",
    ]
    candidates: list[Model] = []
    seen: set[str] = set()
    for model_id in preferred_ids:
        model = MODELS.get(model_id)
        if model is not None and _is_regular_chat_model(model) and not model.id.endswith(":free"):
            candidates.append(model)
            seen.add(model.id)
    for model in cheap_candidate_models(limit=limit * 2):
        if model.id not in seen:
            candidates.append(model)
            seen.add(model.id)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def meta_candidate_models(model_id: str) -> list[Model]:
    if model_id == AUTO_MODEL_ID:
        return auto_candidate_models()
    if model_id == FREE_MODEL_ID:
        return free_candidate_models()
    if model_id == CHEAP_MODEL_ID:
        return cheap_candidate_models()
    if model_id == MONITOR_MODEL_ID:
        return monitor_candidate_models()
    return []


def _meta_route_kind(model_id: str) -> str:
    if model_id == FREE_MODEL_ID:
        return "free_pool"
    if model_id == CHEAP_MODEL_ID:
        return "cheap_pool"
    if model_id == MONITOR_MODEL_ID:
        return "synthetic_monitor_pool"
    if model_id == AUTO_MODEL_ID:
        return "auto_pool"
    return "model"


def _is_regular_chat_model(model: Model) -> bool:
    return model.id not in META_MODEL_IDS and model.supports_chat


def _price_sort_key(model: Model) -> tuple[int, str, str]:
    return (
        model.prompt_price_microdollars_per_million_tokens
        + model.completion_price_microdollars_per_million_tokens,
        model.provider,
        model.id,
    )


def _meta_price_range(
    model_id: str,
    attr: str,
) -> tuple[int, int]:
    """Return (min, max) of the requested price attribute across the
    Auto model's candidate set. Auto itself has no intrinsic price —
    the request lands on whatever model the router picks — so we
    surface the range so /v1/models doesn't show a misleading $0."""
    candidates = meta_candidate_models(model_id)
    values = [getattr(c, attr) for c in candidates if getattr(c, attr, 0) > 0]
    if not values:
        return (0, 0)
    return (min(values), max(values))


def model_to_openrouter_shape(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    is_meta = model.id in META_MODEL_IDS
    endpoints = endpoints_for_model(model.id)
    prepaid_available = (
        any(endpoint.usage_type == "Credits" for endpoint in endpoints) or model.prepaid_available
    )
    byok_available = any(endpoint.usage_type == "BYOK" for endpoint in endpoints) or (
        model.byok_available and PROVIDERS[model.provider].supports_byok
    )

    # For meta routers, derive prompt/completion price from the candidate range
    # rather than the catalog's hard-coded 0. Most OpenRouter-compat
    # consumers (browsers, marketplace listings, billing previews) read
    # `pricing.prompt` / `pricing.completion`; if those are 0, Auto
    # appears free in every dashboard. We report the cheapest candidate
    # as the headline price (matches OpenRouter's convention for their
    # `openrouter/auto` meta-model) and add `*_max` fields plus the
    # full candidate manifest so anything that wants a range can show one.
    prompt_min = model.prompt_price_microdollars_per_million_tokens
    prompt_max = prompt_min
    completion_min = model.completion_price_microdollars_per_million_tokens
    completion_max = completion_min
    pub_prompt_min = model.published_prompt_price_microdollars_per_million_tokens
    pub_prompt_max = pub_prompt_min
    pub_completion_min = model.published_completion_price_microdollars_per_million_tokens
    pub_completion_max = pub_completion_min
    if is_meta:
        prompt_min, prompt_max = _meta_price_range(
            model.id, "prompt_price_microdollars_per_million_tokens"
        )
        completion_min, completion_max = _meta_price_range(
            model.id, "completion_price_microdollars_per_million_tokens"
        )
        pub_prompt_min, pub_prompt_max = _meta_price_range(
            model.id, "published_prompt_price_microdollars_per_million_tokens"
        )
        pub_completion_min, pub_completion_max = _meta_price_range(
            model.id, "published_completion_price_microdollars_per_million_tokens"
        )

    pricing: dict[str, str] = {
        "prompt": microdollars_per_million_tokens_to_token_decimal(prompt_min),
        "completion": microdollars_per_million_tokens_to_token_decimal(completion_min),
    }
    if is_meta and (prompt_max != prompt_min or completion_max != completion_min):
        pricing["prompt_max"] = microdollars_per_million_tokens_to_token_decimal(prompt_max)
        pricing["completion_max"] = microdollars_per_million_tokens_to_token_decimal(completion_max)

    tr_block: dict[str, object] = {
        "provider": model.provider,
        "prepaid_available": prepaid_available,
        "byok_available": byok_available,
        "attested_gateway": provider.attested_gateway,
        "stores_content": provider.stores_content,
        "prompt_price_microdollars_per_million_tokens": prompt_min,
        "completion_price_microdollars_per_million_tokens": completion_min,
        "published_prompt_price_microdollars_per_million_tokens": pub_prompt_min,
        "published_completion_price_microdollars_per_million_tokens": pub_completion_min,
        # Uniform pricing means the customer pays the headline rate — no
        # secret 1¢/M discount layered on top. Field kept for OpenRouter
        # consumer compat, but always zero.
        "discount_microdollars_per_million_tokens": 0,
        "auto_candidates": [c.id for c in meta_candidate_models(model.id)] if is_meta else None,
        "route_kind": _meta_route_kind(model.id) if is_meta else "model",
        "synthetic_monitor": model.id == MONITOR_MODEL_ID,
        "internal_only": model.id == MONITOR_MODEL_ID,
        "endpoints": [
            {
                "id": endpoint.id,
                "provider": endpoint.provider,
                "provider_name": PROVIDERS[endpoint.provider].name,
                "usage_type": endpoint.usage_type,
                "upstream_id": endpoint.upstream_id,
                "attested_gateway": PROVIDERS[endpoint.provider].attested_gateway,
                "stores_content": PROVIDERS[endpoint.provider].stores_content,
            }
            for endpoint in endpoints
        ],
    }
    if is_meta:
        tr_block["prompt_price_max_microdollars_per_million_tokens"] = prompt_max
        tr_block["completion_price_max_microdollars_per_million_tokens"] = completion_max
        tr_block["published_prompt_price_max_microdollars_per_million_tokens"] = pub_prompt_max
        tr_block["published_completion_price_max_microdollars_per_million_tokens"] = (
            pub_completion_max
        )

    return {
        "id": model.id,
        "name": model.name,
        "created": 0,
        "description": f"{model.name} via TrustedRouter",
        "context_length": model.context_length,
        "architecture": {"modality": "text->text", "tokenizer": "unknown", "instruct_type": None},
        "pricing": pricing,
        "top_provider": {
            "context_length": model.context_length,
            "max_completion_tokens": None,
            "is_moderated": False,
        },
        "per_request_limits": None,
        "trustedrouter": tr_block,
    }
