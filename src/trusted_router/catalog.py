from __future__ import annotations

from trusted_router.catalog_data import (  # noqa: F401 - re-exported for back-compat
    _EMBEDDING_SPECS,
    _MODEL_PROVIDER_PRIVACY_OVERRIDES,
    _PROVIDER_DISPLAY_ORDER,
    _PROVIDER_SERVED_MODEL_ALLOWLIST,
    _PROVIDER_UNSERVED_CREDITS_MODELS,
    _UNSERVED_CREDITS_MODELS,
    ADVISOR_CATALOG_MODEL_ORDERS,
    ADVISOR_MODEL_ID,
    ARISTOTLE_1_0_MODEL_ID,
    ARISTOTLE_1_1_MODEL_ID,
    ARISTOTLE_MODEL_ID,
    ATHENA_MODEL_ID,
    AUTO_MODEL_ID,
    CANONICAL_ORCHESTRATION_MODEL_ID,
    CHEAP_MODEL_ID,
    DEFAULT_AUTO_MODEL_ORDER,
    E2E_MODEL_ID,
    EU_FOCUSED_PROVIDER_ORDER,
    EU_MODEL_ID,
    FAST_MODEL_ID,
    FREE_MODEL_ID,
    FUSION_CODE_MODEL_ID,
    FUSION_MODEL_ID,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    IRIS_1_0_MODEL_ID,
    IRIS_CODE_1_0_MODEL_ID,
    IRIS_CODE_MODEL_ID,
    IRIS_MODEL_ID,
    LIBERTY_1_0_1M_MODEL_ID,
    LIBERTY_1_0_1M_MODEL_ORDER,
    LIBERTY_1_0_MODEL_ID,
    LIBERTY_1_0_MODEL_ORDER,
    LIBERTY_2_0_MODEL_ID,
    LIBERTY_3_0_MODEL_ID,
    MAPREDUCE_CATALOG_MODEL_ORDER,
    MAPREDUCE_MODEL_ID,
    META_MODEL_IDS,
    MONITOR_MODEL_ID,
    OPEN_PATCHER_A1_MODEL_ID,
    OPEN_PATCHER_FAST1_MODEL_ID,
    OPEN_PATCHER_G1_MODEL_ID,
    OPEN_PATCHER_G2_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    ORCHESTRATION_LEGACY_ALIAS_MODEL_IDS,
    ORCHESTRATION_PRIMITIVE_BY_MODEL_ID,
    ORCHESTRATION_PRIMITIVE_MODEL_IDS,
    ORCHESTRATION_PRIMITIVE_NAMES,
    ORCHESTRATION_ROLLING_ALIAS_MODEL_IDS,
    PLATO_1_0_MODEL_ID,
    PLATO_MODEL_ID,
    PLATO_PRO_1_0_MODEL_ID,
    PLATO_PRO_MODEL_ID,
    PRIVACY_TIER_ALIASES,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_LABELS,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROMETHEUS_1_0_1M_MODEL_ID,
    PROMETHEUS_1_0_MODEL_ID,
    PROMETHEUS_2_0_MODEL_ID,
    PROMETHEUS_CODE_1_0_MODEL_ID,
    PROMETHEUS_CODE_MODEL_ID,
    PROMETHEUS_MODEL_ID,
    PROVIDER_JURISDICTION_US,
    PROVIDERS,
    SELECTOR_CATALOG_MODEL_ORDER,
    SELECTOR_MODEL_ID,
    SOCRATES_1_0_MODEL_ID,
    SOCRATES_1_1_MODEL_ID,
    SOCRATES_ADVISOR_MODEL_ORDER,
    SOCRATES_CATALOG_MODEL_ORDER,
    SOCRATES_MODEL_ID,
    SOCRATES_PRO_1_0_MODEL_ID,
    SOCRATES_PRO_MODEL_ID,
    SOCRATES_PRO_PLUS_1_0_MODEL_ID,
    SOCRATES_PRO_PLUS_MODEL_ID,
    SOCRATES_WORKER_MODEL_ORDER,
    SUBAGENT_MODEL_ID,
    SYNTH_BUDGET_MODEL_ORDER,
    SYNTH_CODE_BUDGET_MODEL_ORDER,
    SYNTH_CODE_FRONTIER_MODEL_ORDER,
    SYNTH_CODE_MODEL_ID,
    SYNTH_CODE_QUALITY_MODEL_ORDER,
    SYNTH_FRONTIER_MINI_MODEL_ORDER,
    SYNTH_FRONTIER_MODEL_ORDER,
    SYNTH_MODEL_ID,
    SYNTH_PROMETHEUS_2_MODEL_ORDER,
    SYNTH_QUALITY_1M_MODEL_ORDER,
    SYNTH_QUALITY_MODEL_ORDER,
    US_PROVIDER_ONLY_MODEL_IDS,
    ZDR_MODEL_ID,
    ZEUS_1_0_MINI_MODEL_ID,
    ZEUS_1_0_MODEL_ID,
    ZEUS_CODE_1_0_MODEL_ID,
    ZEUS_CODE_MODEL_ID,
    ZEUS_MODEL_ID,
    Model,
    ModelEndpoint,
    ModelProviderPrivacyOverride,
    Provider,
    _EmbeddingSpec,
)
from trusted_router.catalog_ingest import (  # noqa: F401 - used by import-time build below
    _AUTHOR_TO_PROVIDER_SLUG,
    _INGEST_PATH,
    _PROVIDER_DEPRECATED_UPSTREAM_MODELS,
    _PROVIDER_MODELS_DIR,
    _author_provider,
    _build_endpoints,
    _embedding_models,
    _endpoint,
    _filter_unserved_provider_endpoints,
    _ingested_models_and_endpoints,
    _is_provider_deprecated_model,
    _supplemental_provider_models_and_endpoints,
)

# Privacy tiers + routing candidate selection were split into dedicated
# modules (#38); re-exported here so `from trusted_router.catalog import ...`
# keeps working for every existing caller.
from trusted_router.catalog_privacy import (  # noqa: F401 - re-exported for back-compat
    endpoint_privacy_tier,
    model_provider_policy,
    model_provider_policy_url,
    model_provider_privacy_tier,
    model_provider_zero_data_retention,
    provider_privacy_tier,
)
from trusted_router.catalog_registry import (  # noqa: F401 - built there, re-exported
    MODEL_ENDPOINTS,
    MODELS,
)
from trusted_router.money import (
    microdollars_per_million_tokens_to_token_decimal,
)
from trusted_router.pricing import (  # noqa: F401 - re-exported for back-compat
    _CACHE_READ_PRICE_MULTIPLIER,
    _CACHE_WRITE_PRICE_MULTIPLIER,
    _DEFAULT_CACHE_READ_MULTIPLIER,
    _DEFAULT_CACHE_WRITE_MULTIPLIER,
    _PRICE_FLOOR_MICRODOLLARS_PER_M,
    _PRICE_MARKUP_RATIO,
    ModelPricingKwargs,
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
    cache_token_prices_microdollars,
    select_price_tier,
)
from trusted_router.routing_candidates import (  # noqa: F401 - re-exported for back-compat
    InvalidAutoModelOrder,
    _is_regular_chat_model,
    _meta_route_kind,
    _models_for_ids,
    _price_sort_key,
    _privacy_candidate_models,
    auto_candidate_models,
    cheap_candidate_models,
    e2e_candidate_models,
    eu_candidate_models,
    fast_candidate_models,
    free_candidate_models,
    meta_candidate_models,
    monitor_candidate_models,
    socrates_candidate_models,
    validate_auto_model_order,
    zdr_candidate_models,
)

# Uniform pricing: customer pays cost + 10%, floor $0.01/M tokens. Same
# value goes into both `prompt_price_*` and `published_*` — TR no longer
# runs the 1¢/M "discount theater". The floor catches free upstream tiers
# so the catalog never advertises $0/M to end users; $0.01/M is ~10×
# margin over real per-request infra cost (~$0.00001/req on a typical
# 10K-token call), recovered via 10K-tokens × $0.01/M = $0.0001/req.


# ---------------------------------------------------------------------------
# Prompt-cache pricing
#
# The attested gateway reports cache_read_input_tokens /
# cache_creation_input_tokens at settle. Cached tokens are billed as a
# multiple of the endpoint's (already marked-up) prompt price, so the
# uniform x1.10 margin structure is preserved: provider charges
# cost x multiplier, we bill customer_price x multiplier.
#
# Multipliers mirror published provider pricing as of 2026-06:
#   anthropic: cache read 0.1x, 5-minute cache write 1.25x
#              (docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
#   openai:    cached input 0.5x for the gpt-4o family; newer families
#              are CHEAPER (0.1x), so 0.5x is the safe-side bound that
#              never bills below our upstream cost. Tighten per-model
#              when the pricing scrapers track cached rates.
#   gemini/vertex: cached content 0.25x.
# Providers absent from the read table get NO discount (1.0x) until
# their cached pricing is verified — overcharging a discount we can't
# confirm is the safe failure mode for margin, and those providers
# rarely report cache fields anyway. Only Anthropic reports cache
# WRITES; the 1.25x default keeps any future writer safe-side too.


# Vertex is intentionally excluded until TR's GCP project gets the
# Anthropic-on-Vertex / Gemini-on-Vertex quota approvals.

# Providers with a direct prepaid implementation in the attested
# quill-cloud-proxy llm_multi gateway. BYOK endpoints may exist for any
# keyed provider, but Credits endpoints must stay in sync with this set so
# the control plane cannot authorize a prepaid route the enclave cannot
# dispatch.


def orchestration_primitive(model_id: str) -> str | None:
    return ORCHESTRATION_PRIMITIVE_BY_MODEL_ID.get(model_id)


def canonical_orchestration_model_id(model_id: str) -> str | None:
    if model_id not in META_MODEL_IDS:
        return None
    return CANONICAL_ORCHESTRATION_MODEL_ID.get(model_id, model_id)


def orchestration_role(model_id: str) -> str | None:
    if model_id not in META_MODEL_IDS:
        return None
    if model_id in ORCHESTRATION_LEGACY_ALIAS_MODEL_IDS:
        return "legacy_alias"
    if model_id in ORCHESTRATION_ROLLING_ALIAS_MODEL_IDS:
        return "rolling_alias"
    if model_id in ORCHESTRATION_PRIMITIVE_MODEL_IDS:
        return "primitive"
    if model_id in ORCHESTRATION_PRIMITIVE_BY_MODEL_ID:
        return "named_preset"
    return "routing_pool"


# EU-focused routing is a provider policy, not a hard data-residency promise.
# It keeps traffic on the EU regional attested gateway when the caller uses
# that base URL, then prefers European / EU-regionable / privacy-forward
# upstreams. Customers needing contractual residency should still pin a
# provider allowlist in their agreement and request body.
# IDs follow snapshot naming exactly. The picks span the 8 keyed
# providers so `trustedrouter/auto` rolls over across providers if any
# one is down. Each entry must have a provider-direct price in the
# snapshot — OR-only models can no longer reach the catalog (see
# scripts/pricing/refresh.py:_merge_snapshot).
#
# 2026-06 update: OpenAI's GPT-5.4 line (incl. gpt-5.4-nano) and the "-pro"
# tiers 502 on our key — verified via the gateway probe; see
# _PROVIDER_UNSERVED_CREDITS_MODELS. Route auto/monitor callers to
# openai/gpt-4.1-mini, which is served (verified OK) and is the current cheap
# mid-tier model. (gpt-5.5 works too but is the pricey flagship.)


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


def _model_max_privacy_tier(model: Model, endpoints: list[ModelEndpoint]) -> int:
    """Highest privacy tier this model can be routed through. For meta
    models (auto/free/cheap), that's the best tier across the candidate
    pool — NOT the 'trustedrouter' pseudo-provider, which would falsely
    claim confidential for Auto. For regular models, the max across the
    model's own provider plus any serving endpoints."""
    tiers: list[int] = []
    if model.id in META_MODEL_IDS:
        for candidate in meta_candidate_models(model.id):
            tiers.append(model_max_privacy_tier(candidate))
    else:
        for endpoint in endpoints:
            tiers.append(endpoint_privacy_tier(endpoint))
        if not tiers and model.provider in PROVIDERS:
            tiers.append(model_provider_privacy_tier(model.id, model.provider))
    return max(tiers) if tiers else PRIVACY_TIER_STANDARD


def model_max_privacy_tier(model: Model) -> int:
    """Public wrapper: highest privacy tier `model` can be routed through,
    resolving its serving endpoints internally. Used by the router's
    min_privacy filter."""
    return _model_max_privacy_tier(model, endpoints_for_model(model.id))


_OPEN_WEIGHT_PREFIXES = (
    "amd/",
    "deepseek/",
    "google/gemma",
    "meta-llama/",
    "minimax/minimax-m3",
    "moonshotai/kimi",
    "nvidia/nemotron",
    "qwen/",
    "thinkingmachines/",
    "xiaomi/mimo",
    "z-ai/glm",
)
_OPEN_WEIGHT_CONTAINS = (
    "/qwen",
    "gpt-oss",
    "zai-glm",
)


def model_open_weights(model: Model, *, _seen: frozenset[str] = frozenset()) -> bool:
    """Whether a model route is purely open weights.

    For TrustedRouter orchestration models this is recursive: every candidate
    beneath the alias must be open weights. If the graph is empty or cycles, we
    fail closed and do not show the badge.
    """
    model_id = model.id.lower()
    if model.hidden_public_metadata:
        return False
    if model.id in _seen:
        return False
    if model.id in META_MODEL_IDS:
        candidates = meta_candidate_models(model.id)
        if not candidates:
            return False
        next_seen = frozenset((*_seen, model.id))
        return all(model_open_weights(candidate, _seen=next_seen) for candidate in candidates)
    return model_id.startswith(_OPEN_WEIGHT_PREFIXES) or any(
        marker in model_id for marker in _OPEN_WEIGHT_CONTAINS
    )


def _route_provider_slugs(
    model: Model,
    endpoints: list[ModelEndpoint],
    *,
    _seen: frozenset[str] = frozenset(),
) -> set[str]:
    """Serving provider slugs reachable for this model.

    For public orchestration aliases, recurse into their component models so
    badges/filters describe the actual route pool. For hidden orchestration
    presets, stop at TrustedRouter so we do not reveal private configuration.
    """
    if model.hidden_public_metadata:
        return {model.provider}
    if model.id in _seen:
        return set()
    if model.id in META_MODEL_IDS:
        next_seen = frozenset((*_seen, model.id))
        providers: set[str] = set()
        for candidate in meta_candidate_models(model.id):
            providers.update(
                _route_provider_slugs(
                    candidate,
                    endpoints_for_model(candidate.id),
                    _seen=next_seen,
                )
            )
        return providers
    providers = {endpoint.provider for endpoint in endpoints}
    if not providers and model.provider in PROVIDERS:
        providers.add(model.provider)
    return providers


def model_us_provider_available(model: Model) -> bool:
    provider_slugs = _route_provider_slugs(model, endpoints_for_model(model.id))
    return any(
        PROVIDERS[slug].provider_headquarters_country == PROVIDER_JURISDICTION_US
        for slug in provider_slugs
        if slug in PROVIDERS
    )


def model_eu_focused_provider_available(model: Model) -> bool:
    provider_slugs = _route_provider_slugs(model, endpoints_for_model(model.id))
    return model.id == EU_MODEL_ID or any(
        slug in EU_FOCUSED_PROVIDER_ORDER for slug in provider_slugs
    )


def model_to_openrouter_shape(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    is_meta = model.id in META_MODEL_IDS
    endpoints = endpoints_for_model(model.id)
    prepaid_available = (
        any(endpoint.usage_type == "Credits" for endpoint in endpoints) or model.prepaid_available
    )
    byok_available = (
        False
        if is_meta
        else (
            any(endpoint.usage_type == "BYOK" for endpoint in endpoints)
            or (model.byok_available and PROVIDERS[model.provider].supports_byok)
        )
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

    auto_candidates = None
    route_kind = _meta_route_kind(model.id) if is_meta else "model"
    if is_meta and not model.hidden_public_metadata:
        auto_candidates = [c.id for c in meta_candidate_models(model.id)]
    if model.hidden_public_metadata:
        route_kind = "private_orchestration"

    tr_block: dict[str, object] = {
        "provider": model.provider,
        "prepaid_available": prepaid_available,
        "byok_available": byok_available,
        "attested_gateway": provider.attested_gateway,
        # Gateway-scoped OpenRouter-compat flag: TrustedRouter does not
        # retain prompt/output content. Upstream provider retention still
        # varies and is exposed per endpoint below plus provider_* fields.
        "stores_content": False,
        "provider_zero_data_retention": model_provider_zero_data_retention(
            model.id, model.provider
        ),
        "provider_confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "provider_policy": model_provider_policy(model.id, model.provider),
        "provider_policy_url": model_provider_policy_url(model.id, model.provider),
        "provider_headquarters_country": provider.provider_headquarters_country,
        "provider_us_based": provider.provider_headquarters_country == PROVIDER_JURISDICTION_US,
        "us_provider_available": model_us_provider_available(model),
        "eu_focused_provider_available": model_eu_focused_provider_available(model),
        "required_provider_jurisdiction": (
            PROVIDER_JURISDICTION_US if model.id in US_PROVIDER_ONLY_MODEL_IDS else None
        ),
        # Highest privacy tier reachable for this model — the max across
        # every provider that serves it (a request can route to the best
        # one). Lets the picker / SEO pages show "this model can run
        # confidential" without re-deriving from raw posture flags.
        "privacy_tier": _model_max_privacy_tier(model, endpoints),
        "privacy_tier_label": PRIVACY_TIER_LABELS[_model_max_privacy_tier(model, endpoints)],
        "open_weights": model_open_weights(model),
        "prompt_price_microdollars_per_million_tokens": prompt_min,
        "completion_price_microdollars_per_million_tokens": completion_min,
        "published_prompt_price_microdollars_per_million_tokens": pub_prompt_min,
        "published_completion_price_microdollars_per_million_tokens": pub_completion_min,
        # Uniform pricing means the customer pays the headline rate — no
        # secret 1¢/M discount layered on top. Field kept for OpenRouter
        # consumer compat, but always zero.
        "discount_microdollars_per_million_tokens": 0,
        "auto_candidates": auto_candidates,
        "route_kind": route_kind,
        "orchestration_primitive": orchestration_primitive(model.id),
        "orchestration_role": orchestration_role(model.id),
        "canonical_model_id": canonical_orchestration_model_id(model.id),
        "configuration_hidden": model.hidden_public_metadata,
        "synthetic_monitor": model.id == MONITOR_MODEL_ID,
        "internal_only": model.id == MONITOR_MODEL_ID,
        # Capability flags so OpenRouter-compat clients (and TR's own chat
        # picker) can tell an embedding model from a chat model without
        # parsing `architecture.modality`.
        "supports_chat": model.supports_chat,
        "supports_embeddings": model.supports_embeddings,
        "endpoints": [
            {
                "id": endpoint.id,
                "provider": endpoint.provider,
                "provider_name": PROVIDERS[endpoint.provider].name,
                "usage_type": endpoint.usage_type,
                "upstream_id": endpoint.upstream_id,
                "attested_gateway": PROVIDERS[endpoint.provider].attested_gateway,
                "stores_content": PROVIDERS[endpoint.provider].stores_content,
                "provider_zero_data_retention": model_provider_zero_data_retention(
                    endpoint.model_id, endpoint.provider
                ),
                "provider_confidential_compute": PROVIDERS[
                    endpoint.provider
                ].provider_confidential_compute,
                "provider_e2ee": PROVIDERS[endpoint.provider].provider_e2ee,
                "provider_policy": model_provider_policy(endpoint.model_id, endpoint.provider),
                "provider_policy_url": model_provider_policy_url(
                    endpoint.model_id, endpoint.provider
                ),
                "provider_headquarters_country": PROVIDERS[
                    endpoint.provider
                ].provider_headquarters_country,
                "provider_us_based": PROVIDERS[endpoint.provider].provider_headquarters_country
                == PROVIDER_JURISDICTION_US,
                "provider_eu_focused": endpoint.provider in EU_FOCUSED_PROVIDER_ORDER,
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
        "architecture": {
            "modality": (
                "text->embedding"
                if model.supports_embeddings and not model.supports_chat
                else "text->text"
            ),
            "tokenizer": "unknown",
            "instruct_type": None,
        },
        "pricing": pricing,
        "top_provider": {
            "context_length": model.context_length,
            "max_completion_tokens": None,
            "is_moderated": False,
        },
        "per_request_limits": None,
        "trustedrouter": tr_block,
    }


def provider_to_openrouter_shape(provider: Provider) -> dict[str, object]:
    return {
        "id": provider.slug,
        "name": provider.name,
        "supports_prepaid": provider.supports_prepaid,
        "supports_byok": provider.supports_byok,
        "attested_gateway": provider.attested_gateway,
        "stores_content": provider.stores_content,
        "provider_zero_data_retention": provider.provider_zero_data_retention,
        "provider_confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "provider_policy": provider.provider_policy,
        "provider_policy_url": provider.provider_policy_url,
        "provider_headquarters_country": provider.provider_headquarters_country,
        "provider_us_based": provider.provider_headquarters_country == PROVIDER_JURISDICTION_US,
    }


def providers_for_display() -> tuple[Provider, ...]:
    """Provider transparency should lead with privacy-forward upstreams."""
    pinned = [PROVIDERS[slug] for slug in _PROVIDER_DISPLAY_ORDER if slug in PROVIDERS]
    pinned_slugs = {provider.slug for provider in pinned}
    return tuple(
        pinned + [provider for provider in PROVIDERS.values() if provider.slug not in pinned_slugs]
    )
