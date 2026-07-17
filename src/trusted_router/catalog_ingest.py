"""Import-time catalog ingestion: builds the model + endpoint registries from
the OpenRouter snapshot and the provider-native manifests, and seeds embeddings.

Extracted from catalog.py (#38). Pure producers — they take the static data +
pricing helpers and RETURN model/endpoint dicts; they never read the live
MODELS/MODEL_ENDPOINTS registries (catalog.py owns those and does the import-time
merge). No dependency on catalog.py, so no import cycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trusted_router.catalog_data import (
    _EMBEDDING_SPECS,
    _PROVIDER_SERVED_MODEL_ALLOWLIST,
    _PROVIDER_UNSERVED_CREDITS_MODELS,
    _UNSERVED_CREDITS_MODELS,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    META_MODEL_IDS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    _EmbeddingSpec,
)
from trusted_router.pricing import (
    PriceTier,
    _as_positive_int,
    _customer_price,
    _customer_price_from_dollars_per_token,
    _flat_tier,
    _priced,
    _provider_manifest_price_cost,
    _provider_manifest_price_scale,
    _provider_manifest_price_tiers,
    _read_pricing_tiers,
)
from trusted_router.provider_lifecycle import provider_model_retired


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


_INGEST_PATH = Path(__file__).parent / "data" / "openrouter_snapshot.json"

_PROVIDER_MODELS_DIR = Path(__file__).parent / "data" / "provider_models"

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
    "fireworks": "fireworks",
    "x-ai": "grok",
    "xai": "grok",
    "xiaomi": "xiaomi",
    "phala": "phala",
    # Keep Meta Llama's primary TR route on Cerebras even when the
    # OpenRouter endpoint snapshot temporarily exposes only a different
    # host. Cerebras is one of TR's direct prepaid/BYOK providers and
    # the gateway can call this upstream model id directly.
    "meta-llama": "cerebras",
    # `qwen/*`, `minimax/*` etc. fall back to whichever endpoint
    # provider serves them — Novita / SiliconFlow and others host
    # open-weight variants, and the endpoint provider determines which
    # TR-keyed provider answers the call.
}

_PROVIDER_DEPRECATED_UPSTREAM_MODELS: dict[str, frozenset[str]] = {
    # Xiaomi retired the MiMo V2 family upstream on 2026-06-29; these manifest
    # rows are historical fallback metadata and have shown 100% probe failure
    # since 2026-06-29. Keep provider-scoped: V2.5 Xiaomi routes remain alive.
    "xiaomi": frozenset(
        {
            "xiaomi/mimo-v2-flash",
            "mimo-v2-flash",
            "xiaomi/mimo-v2-pro",
            "mimo-v2-pro",
        }
    ),
    # Nebius notified customers that these Token Factory model APIs / UI
    # entries will be disabled on 2026-06-22. This is provider-scoped:
    # equivalent model families on MiniMax, Kimi, Z.AI, Cerebras, etc. remain
    # routable if those providers still serve them. Drop both prepaid and BYOK
    # Nebius endpoints because the upstream model API itself is going away.
    "nebius": frozenset(
        {
            "deepseek-ai/DeepSeek-V3.2",
            "deepseek-ai/DeepSeek-V3.2-fast",
            "MiniMaxAI/MiniMax-M2.5-fast",
            "moonshotai/Kimi-K2.5",
            "moonshotai/Kimi-K2.5-fast",
            "openai/gpt-oss-120b-fast",
            "PrimeIntellect/INTELLECT-3",
            "Qwen/Qwen3-235B-A22B-Thinking-2507-fast",
            "Qwen/Qwen3-Next-80B-A3B-Thinking-fast",
            "Qwen/Qwen3.5-397B-A17B-fast",
            "zai-org/GLM-5",
        }
    ),
    # Tinfoil notified users that GLM 5.1 and Qwen3-VL-30B are deprecated on
    # 2026-06-22. Keep this provider-scoped: GLM 5.1 / Qwen routes on other
    # providers are unaffected, while Tinfoil callers should move to glm-5-2
    # and gemma4-31b respectively.
    "tinfoil": frozenset(
        {
            "z-ai/glm-5.1",
            "glm-5-1",
            "qwen/qwen3-vl-30b",
            "qwen/qwen3-vl-30b-a3b-instruct",
            "qwen3-vl-30b",
        }
    ),
    # Novita notified customers that these DeepSeek and Qwen model APIs retire
    # on 2026-07-01 00:00 UTC. Replacement routes are deepseek-v4-flash,
    # qwen3.6-27b, and qwen3.6-35b-a3b. This is provider-scoped: the same
    # model ids on other providers remain routable if those providers still
    # serve them.
    "novita": frozenset(
        {
            "deepseek/deepseek-r1-distill-qwen-14b",
            "deepseek/deepseek-r1-distill-qwen-32b",
            "qwen/qwen3-14b",
            "qwen/qwen3-30b-a3b",
            "qwen/qwen3-30b-a3b-instruct-2507",
            "qwen/qwen3-30b-a3b-thinking-2507",
            "qwen/qwen3-32b",
            "qwen/qwen3-8b",
            "qwen/qwen3-next-80b-a3b-thinking",
            "qwen/qwen3-vl-30b-a3b-thinking",
            "qwen/qwen3-vl-32b-instruct",
            "qwen/qwen3-vl-32b-thinking",
            "qwen/qwen3-vl-8b-instruct",
            "qwen/qwen3-vl-8b-thinking",
            # 100% MODEL_NOT_AVAILABLE-class probe failures since 2026-06-05.
            "baidu/ernie-4.5-vl-28b-a3b",
            # 100% MODEL_NOT_AVAILABLE-class probe failures since 2026-06-23.
            "meta-llama/llama-3-70b-instruct",
            # route-health first sweep 2026-07-18, 100% failure.
            "zai-org/glm-4.5",
        }
    ),
    # Friendli notified customers that GLM-5 serverless Model APIs stop being
    # supported at 2026-07-03 00:00 UTC. Dedicated endpoints are unaffected, but
    # TrustedRouter's Friendli route is the serverless API, so remove only this
    # provider/model pair from routable candidates. Friendli also dropped
    # Llama 3.3 70B from serverless /models around 2026-06-26; it has shown
    # 100% probe failure since 2026-06-26.
    "friendli": frozenset(
        {
            "z-ai/glm-5",
            "zai-org/GLM-5",
            "meta-llama/llama-3.3-70b-instruct",
            "meta-llama-3.3-70b-instruct",
        }
    ),
    # Google retired the Gemini 3.1 Flash Lite preview id on 2026-07-09; the
    # direct Gemini preview route has shown 100% probe failure since then. GA
    # flash-lite routes on reseller providers are unaffected.
    "gemini": frozenset(
        {
            "google/gemini-3.1-flash-lite-preview",
            "gemini-3.1-flash-lite-preview",
        }
    ),
    # Makora's AMD Llama 3.3 70B FP8 KV row was added on 2026-07-03 but never
    # served a request; probes hang or 502, giving 100% failure since 2026-07-03.
    "makora": frozenset(
        {
            "amd/llama-3.3-70b-instruct-fp8-kv",
            "amd/Llama-3.3-70B-Instruct-FP8-KV",
            # route-health first sweep 2026-07-18, 100% failure.
            "qwen/qwen3.6-27b",
            "openai/gpt-oss-120b",
        }
    ),
    # These resellers list Claude ids their APIs do not actually serve: 100%
    # provider_error in synthetic probes since the first sample, verified
    # 2026-07-17. Anthropic-direct and Lightning routes are unaffected.
    "gmi": frozenset(
        {
            "anthropic/claude-fable-5",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.1",
            # route-health first sweep 2026-07-18, 100% failure.
            "x-ai/grok-4.5",
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-5.6-luna",
            "qwen/qwen3.5-27b",
            "google/gemini-3-flash-preview",
        }
    ),
    "deepinfra": frozenset(
        {
            "anthropic/claude-fable-5",
            "anthropic/claude-sonnet-5",
            # route-health flag 2026-07-18 (post-first-sweep): 100% failure over 6 samples.
            "google/gemini-3.5-flash",
            # route-health first sweep 2026-07-18, 100% failure.
            "z-ai/glm-5.1",
            "moonshotai/kimi-k2.7-code",
            "qwen/qwen3-32b",
        }
    ),
    "phala": frozenset(
        {
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.1",
            # route-health first sweep 2026-07-18, 100% failure.
            "anthropic/claude-opus-4.8",
            "deepseek/deepseek-v4-pro",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure. These ids may be
    # remappable to newer upstream ids — revisit with per-provider manifest hooks.
    "together": frozenset(
        {
            "openai/gpt-oss-120b",
            "deepseek/deepseek-r1-distill-llama-70b",
            "qwen/qwen3-vl-8b-instruct",
            "qwen/qwen2.5-vl-72b-instruct",
            "mistralai/mistral-small-24b-instruct-2501",
            "moonshotai/kimi-k2.7-code",
            "minimax/minimax-m3",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "lightning": frozenset(
        {
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-luna",
            "openai/gpt-4-turbo-preview",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure. These ids may be
    # remappable to newer upstream ids — revisit with per-provider manifest hooks.
    "kimi": frozenset(
        {
            "moonshotai/kimi-k2",
            "moonshotai/kimi-k2-0905",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "openai": frozenset(
        {
            "openai/gpt-4-turbo-preview",
            "openai/gpt-5.2-chat",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "mistral": frozenset(
        {
            "mistralai/mistral-small-24b-instruct-2501",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "meta": frozenset(
        {
            "meta/muse-spark-1.1",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "deepseek": frozenset(
        {
            "deepseek/deepseek-v3.1-terminus",
        }
    ),
}


def _is_provider_deprecated_model(
    provider_slug: str,
    model_id: str,
    upstream_id: str | None,
) -> bool:
    if provider_model_retired(provider_slug, model_id, upstream_id):
        return True
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS.get(provider_slug)
    if not deprecated:
        return False
    return model_id in deprecated or (upstream_id is not None and upstream_id in deprecated)


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
            upstream_id = str(raw_ep.get("model_id") or model_id)
            if _is_provider_deprecated_model(slug, model_id, upstream_id):
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


def _supplemental_provider_models_and_endpoints() -> tuple[
    dict[str, Model], dict[str, ModelEndpoint]
]:
    """Read provider-native model manifests for providers whose live API
    lists more routes than OpenRouter's endpoint feed. These manifests
    preserve exact upstream model IDs and authoritative downstream prices, so
    the control plane can authorize routes the attested gateway can actually
    call and bill. Most are provider-direct; Meta Muse is explicitly labelled
    as Meta via OpenRouter.

    Novita, Nebius, MiniMax, Crusoe, Cerebras, Gemini, Fireworks, DeepInfra,
    Moonshot/Kimi, and Z.AI currently use this path because their
    live `/models` feeds expose working provider-direct routes before
    OpenRouter's public endpoint catalog catches up. Anthropic uses it for
    Claude Opus 4.8, which shipped after the snapshot — the attested gateway
    maps `anthropic/claude-opus-4.8` -> `claude-opus-4-8` algorithmically
    (internal/llm/anthropic.go), so the route works with no enclave change.
    """
    models: dict[str, Model] = {}
    endpoints: dict[str, ModelEndpoint] = {}
    for provider_slug in (
        "novita",
        "nebius",
        "minimax",
        "anthropic",
        "cerebras",
        "gemini",
        "fireworks",
        "deepinfra",
        "grok",
        "gmi",
        "together",
        "phala",
        "siliconflow",
        "venice",
        "parasail",
        "friendli",
        "baseten",
        "thinkingmachines",
        "wafer",
        "crusoe",
        "makora",
        "kimi",
        "zai",
        "tinfoil",
        "xiaomi",
        "meta",
    ):
        path = _PROVIDER_MODELS_DIR / f"{provider_slug}.json"
        if not path.exists() or provider_slug not in PROVIDERS:
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw_models = raw.get("models")
        if not isinstance(raw_models, list):
            continue
        provider = PROVIDERS[provider_slug]
        price_scale = _provider_manifest_price_scale(raw)
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            # Discovery-only metadata rows must never produce catalog routes.
            if raw_model.get("routable") is False:
                continue
            model_id = raw_model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            upstream_id = raw_model.get("upstream_id")
            if not isinstance(upstream_id, str) or not upstream_id:
                upstream_id = model_id
            if _is_provider_deprecated_model(provider_slug, model_id, upstream_id):
                continue
            if raw_model.get("model_type") not in (None, "chat"):
                continue
            if "chat/completions" not in {str(item) for item in (raw_model.get("endpoints") or [])}:
                continue

            prompt_cost = _provider_manifest_price_cost(
                raw_model.get("input_token_price_per_m"),
                price_scale=price_scale,
            )
            completion_cost = _provider_manifest_price_cost(
                raw_model.get("output_token_price_per_m"),
                price_scale=price_scale,
            )
            cached_cost = _provider_manifest_price_cost(
                raw_model.get("cached_input_token_price_per_m"),
                price_scale=price_scale,
            )
            prompt_price = _customer_price(prompt_cost)
            completion_price = _customer_price(completion_cost)
            cached_price = _customer_price(cached_cost) if cached_cost > 0 else None
            tiers = _provider_manifest_price_tiers(
                raw_model,
                prompt_price,
                completion_price,
                cached_price,
                price_scale=price_scale,
            )
            publisher = (
                _author_provider(model_id, [{"tr_provider_slug": provider_slug}]) or provider_slug
            )
            context_length = _as_positive_int(raw_model.get("context_length"))
            name = str(raw_model.get("display_name") or raw_model.get("title") or model_id)

            model = Model(
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
            models.setdefault(model_id, model)

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


def _embedding_models() -> dict[str, Model]:
    """Seed the embedding-model catalog (input-only pricing).

    Provider manifests override the checked-in fallback price when an hourly
    first-party parser has produced a current embedding row. Static specs
    remain the last-known-good fallback for providers that do not publish a
    parseable price source.
    """
    models: dict[str, Model] = {}
    for spec in _EMBEDDING_SPECS:
        if spec["provider"] not in PROVIDERS:
            continue
        manifest_cost = _embedding_manifest_cost(spec)
        if manifest_cost is None:
            prompt_price, published_price, _cost = _priced(
                spec["cost_dollars_per_million"]
            )
        else:
            prompt_price = _customer_price(manifest_cost)
            published_price = prompt_price
        models[spec["id"]] = Model(
            id=spec["id"],
            name=spec["name"],
            provider=spec["provider"],
            context_length=spec["context_length"],
            upstream_id=spec["upstream_id"],
            supports_chat=False,
            supports_messages=False,
            supports_embeddings=True,
            prepaid_available=True,
            byok_available=True,
            prompt_price_microdollars_per_million_tokens=prompt_price,
            completion_price_microdollars_per_million_tokens=0,
            published_prompt_price_microdollars_per_million_tokens=published_price,
            published_completion_price_microdollars_per_million_tokens=0,
            price_tiers=_flat_tier(prompt_price, 0, None),
            published_price_tiers=_flat_tier(published_price, 0, None),
        )
    return models


def _embedding_manifest_cost(spec: _EmbeddingSpec) -> int | None:
    """Return a provider-manifest input cost in microdollars/M, if valid."""
    path = _PROVIDER_MODELS_DIR / f"{spec['provider']}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = raw.get("models")
    if not isinstance(rows, list):
        return None
    price_scale = _provider_manifest_price_scale(raw)
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != spec["id"]:
            continue
        if row.get("model_type") != "embedding":
            return None
        if "embeddings" not in {str(item) for item in (row.get("endpoints") or [])}:
            return None
        cost = _provider_manifest_price_cost(
            row.get("input_token_price_per_m"),
            price_scale=price_scale,
        )
        return cost if cost > 0 else None
    return None


def _filter_unserved_provider_endpoints(
    endpoints: dict[str, ModelEndpoint],
) -> dict[str, ModelEndpoint]:
    """Drop a provider's prepaid (Credits) endpoints for models it doesn't
    serve on our account. Only Credits routes use OUR provider key, so only
    those 502 on an account mismatch — BYOK routes use the customer's own key
    (their account may serve a different model set), so they're left intact.

    Four complementary filters apply:
      * provider deprecation — drop a disabled upstream route on one provider for
        every usage type (Nebius June 2026 retirements).
      * allowlist        — keep ONLY the listed Credits models for a provider (Cerebras).
      * model denylist    — drop the listed Credits models on EVERY provider (GPT-5.4/pro).
      * provider denylist — drop a Credits model on ONE provider only (gmi closed models).
    """
    allow = _PROVIDER_SERVED_MODEL_ALLOWLIST

    def _keep(endpoint: ModelEndpoint) -> bool:
        if _is_provider_deprecated_model(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id
        ):
            return False
        if endpoint.usage_type != "Credits":
            return True
        if endpoint.provider in allow and endpoint.model_id not in allow[endpoint.provider]:
            return False
        if endpoint.model_id in _UNSERVED_CREDITS_MODELS:
            return False
        if endpoint.model_id in _PROVIDER_UNSERVED_CREDITS_MODELS.get(
            endpoint.provider, frozenset()
        ):
            return False
        return True

    return {endpoint_id: endpoint for endpoint_id, endpoint in endpoints.items() if _keep(endpoint)}
