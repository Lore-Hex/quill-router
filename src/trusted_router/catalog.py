from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    MAPREDUCE_CATALOG_MODEL_ORDER,
    MAPREDUCE_MODEL_ID,
    META_MODEL_IDS,
    MONITOR_MODEL_ID,
    OPEN_PATCHER_A1_MODEL_ID,
    OPEN_PATCHER_FAST1_MODEL_ID,
    OPEN_PATCHER_G1_MODEL_ID,
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

# Privacy-posture tier logic (the PRIVACY_TIER_* ranks + aliases these read now
# live in catalog_data; these functions apply them).





def provider_privacy_tier(provider: Provider) -> int:
    """The highest privacy bar a provider clears. Used to enforce a
    request's minimum-privacy routing preference. Note the TR gateway hop
    is always attested regardless of tier — this rank is about the
    UPSTREAM provider's posture, which is what varies."""
    if provider.provider_confidential_compute and provider.provider_e2ee:
        return PRIVACY_TIER_CONFIDENTIAL
    if provider.provider_zero_data_retention:
        return PRIVACY_TIER_ZERO_RETENTION
    if provider.stores_content is False:
        return PRIVACY_TIER_NO_STORE
    return PRIVACY_TIER_STANDARD






def model_provider_privacy_tier(model_id: str, provider_slug: str) -> int:
    override = _MODEL_PROVIDER_PRIVACY_OVERRIDES.get((model_id, provider_slug))
    if override is not None:
        return override.privacy_tier
    return provider_privacy_tier(PROVIDERS[provider_slug])


def endpoint_privacy_tier(endpoint: ModelEndpoint) -> int:
    return model_provider_privacy_tier(endpoint.model_id, endpoint.provider)


def model_provider_zero_data_retention(model_id: str, provider_slug: str) -> bool | None:
    override = _MODEL_PROVIDER_PRIVACY_OVERRIDES.get((model_id, provider_slug))
    if override is not None and override.provider_zero_data_retention is not None:
        return override.provider_zero_data_retention
    return PROVIDERS[provider_slug].provider_zero_data_retention


def model_provider_policy(model_id: str, provider_slug: str) -> str:
    override = _MODEL_PROVIDER_PRIVACY_OVERRIDES.get((model_id, provider_slug))
    if override is not None and override.provider_policy is not None:
        return override.provider_policy
    return PROVIDERS[provider_slug].provider_policy


def model_provider_policy_url(model_id: str, provider_slug: str) -> str | None:
    override = _MODEL_PROVIDER_PRIVACY_OVERRIDES.get((model_id, provider_slug))
    if override is not None and override.provider_policy_url is not None:
        return override.provider_policy_url
    return PROVIDERS[provider_slug].provider_policy_url














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
    FAST_MODEL_ID: Model(
        id=FAST_MODEL_ID,
        name="TrustedRouter Fast",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    EU_MODEL_ID: Model(
        id=EU_MODEL_ID,
        name="TrustedRouter EU",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ZDR_MODEL_ID: Model(
        id=ZDR_MODEL_ID,
        name="TrustedRouter ZDR",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    E2E_MODEL_ID: Model(
        id=E2E_MODEL_ID,
        name="TrustedRouter E2E",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
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
    SOCRATES_1_0_MODEL_ID: Model(
        id=SOCRATES_1_0_MODEL_ID,
        name="TrustedRouter Socrates 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SOCRATES_1_1_MODEL_ID: Model(
        id=SOCRATES_1_1_MODEL_ID,
        name="TrustedRouter Socrates 1.1",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SOCRATES_MODEL_ID: Model(
        id=SOCRATES_MODEL_ID,
        name="TrustedRouter Socrates",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ADVISOR_MODEL_ID: Model(
        id=ADVISOR_MODEL_ID,
        name="TrustedRouter Advisor",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SUBAGENT_MODEL_ID: Model(
        id=SUBAGENT_MODEL_ID,
        name="TrustedRouter Subagent",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ARISTOTLE_1_0_MODEL_ID: Model(
        id=ARISTOTLE_1_0_MODEL_ID,
        name="TrustedRouter Aristotle 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ARISTOTLE_MODEL_ID: Model(
        id=ARISTOTLE_MODEL_ID,
        name="TrustedRouter Aristotle",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PLATO_1_0_MODEL_ID: Model(
        id=PLATO_1_0_MODEL_ID,
        name="TrustedRouter Plato 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PLATO_MODEL_ID: Model(
        id=PLATO_MODEL_ID,
        name="TrustedRouter Plato",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PLATO_PRO_1_0_MODEL_ID: Model(
        id=PLATO_PRO_1_0_MODEL_ID,
        name="TrustedRouter Plato Pro 1.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PLATO_PRO_MODEL_ID: Model(
        id=PLATO_PRO_MODEL_ID,
        name="TrustedRouter Plato Pro",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SOCRATES_PRO_1_0_MODEL_ID: Model(
        id=SOCRATES_PRO_1_0_MODEL_ID,
        name="TrustedRouter Socrates Pro 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SOCRATES_PRO_MODEL_ID: Model(
        id=SOCRATES_PRO_MODEL_ID,
        name="TrustedRouter Socrates Pro",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SOCRATES_PRO_PLUS_1_0_MODEL_ID: Model(
        id=SOCRATES_PRO_PLUS_1_0_MODEL_ID,
        name="TrustedRouter Socrates Pro Plus 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SOCRATES_PRO_PLUS_MODEL_ID: Model(
        id=SOCRATES_PRO_PLUS_MODEL_ID,
        name="TrustedRouter Socrates Pro Plus",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    OPEN_PATCHER_S1_MODEL_ID: Model(
        id=OPEN_PATCHER_S1_MODEL_ID,
        name="TrustedRouter OpenPatcher-S1",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    OPEN_PATCHER_A1_MODEL_ID: Model(
        id=OPEN_PATCHER_A1_MODEL_ID,
        name="TrustedRouter OpenPatcher-A1",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    OPEN_PATCHER_FAST1_MODEL_ID: Model(
        id=OPEN_PATCHER_FAST1_MODEL_ID,
        name="TrustedRouter OpenPatcher-Fast1",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    OPEN_PATCHER_G1_MODEL_ID: Model(
        id=OPEN_PATCHER_G1_MODEL_ID,
        name="TrustedRouter OpenPatcher-G1",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ATHENA_MODEL_ID: Model(
        id=ATHENA_MODEL_ID,
        name="TrustedRouter Athena",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
        hidden_public_metadata=True,
    ),
    SYNTH_MODEL_ID: Model(
        id=SYNTH_MODEL_ID,
        name="TrustedRouter Synth",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    IRIS_MODEL_ID: Model(
        id=IRIS_MODEL_ID,
        name="TrustedRouter Iris",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PROMETHEUS_MODEL_ID: Model(
        id=PROMETHEUS_MODEL_ID,
        name="TrustedRouter Prometheus",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ZEUS_MODEL_ID: Model(
        id=ZEUS_MODEL_ID,
        name="TrustedRouter Zeus",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    IRIS_1_0_MODEL_ID: Model(
        id=IRIS_1_0_MODEL_ID,
        name="TrustedRouter Iris 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PROMETHEUS_1_0_MODEL_ID: Model(
        id=PROMETHEUS_1_0_MODEL_ID,
        name="TrustedRouter Prometheus 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PROMETHEUS_1_0_1M_MODEL_ID: Model(
        id=PROMETHEUS_1_0_1M_MODEL_ID,
        name="TrustedRouter Prometheus 1.0 1M",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ZEUS_1_0_MODEL_ID: Model(
        id=ZEUS_1_0_MODEL_ID,
        name="TrustedRouter Zeus 1.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ZEUS_1_0_MINI_MODEL_ID: Model(
        id=ZEUS_1_0_MINI_MODEL_ID,
        name="TrustedRouter Zeus 1.0 Mini",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SYNTH_CODE_MODEL_ID: Model(
        id=SYNTH_CODE_MODEL_ID,
        name="TrustedRouter Synth Code",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    IRIS_CODE_MODEL_ID: Model(
        id=IRIS_CODE_MODEL_ID,
        name="TrustedRouter Iris Code",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PROMETHEUS_CODE_MODEL_ID: Model(
        id=PROMETHEUS_CODE_MODEL_ID,
        name="TrustedRouter Prometheus Code",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ZEUS_CODE_MODEL_ID: Model(
        id=ZEUS_CODE_MODEL_ID,
        name="TrustedRouter Zeus Code",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    IRIS_CODE_1_0_MODEL_ID: Model(
        id=IRIS_CODE_1_0_MODEL_ID,
        name="TrustedRouter Iris Code 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PROMETHEUS_CODE_1_0_MODEL_ID: Model(
        id=PROMETHEUS_CODE_1_0_MODEL_ID,
        name="TrustedRouter Prometheus Code 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ZEUS_CODE_1_0_MODEL_ID: Model(
        id=ZEUS_CODE_1_0_MODEL_ID,
        name="TrustedRouter Zeus Code 1.0",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    FUSION_MODEL_ID: Model(
        id=FUSION_MODEL_ID,
        name="TrustedRouter Synth Legacy Alias",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    FUSION_CODE_MODEL_ID: Model(
        id=FUSION_CODE_MODEL_ID,
        name="TrustedRouter Synth Code Legacy Alias",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    SELECTOR_MODEL_ID: Model(
        id=SELECTOR_MODEL_ID,
        name="TrustedRouter Selector",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    MAPREDUCE_MODEL_ID: Model(
        id=MAPREDUCE_MODEL_ID,
        name="TrustedRouter MapReduce",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
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
    "fireworks": "fireworks",
    "x-ai": "grok",
    "xai": "grok",
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
        }
    ),
    # Friendli notified customers that GLM-5 serverless Model APIs stop being
    # supported at 2026-07-03 00:00 UTC. Dedicated endpoints are unaffected, but
    # TrustedRouter's Friendli route is the serverless API, so remove only this
    # provider/model pair from routable candidates.
    "friendli": frozenset({"z-ai/glm-5", "zai-org/GLM-5"}),
}


def _is_provider_deprecated_model(
    provider_slug: str,
    model_id: str,
    upstream_id: str | None,
) -> bool:
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
    preserve exact upstream model IDs and provider-direct prices, so the
    control plane can authorize routes the attested gateway can actually
    call and bill.

    Novita, Nebius, MiniMax, Crusoe, Cerebras, Gemini, Fireworks, DeepInfra, and Z.AI currently use this path because their
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
        "gmi",
        "together",
        "phala",
        "siliconflow",
        "venice",
        "parasail",
        "friendli",
        "baseten",
        "wafer",
        "crusoe",
        "zai",
        "tinfoil",
        "xiaomi",
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


# --- Embedding models -----------------------------------------------------
# Hand-curated embedding catalog. Embeddings don't come from the OpenRouter
# chat snapshot or the chat-only provider manifests (`_supplemental_*` skips
# any model_type != chat), so they're seeded here with explicit upstream IDs
# + published per-million INPUT prices. Completion price is always 0 —
# embeddings bill input tokens only. Each model gets a Credits endpoint (if
# its provider is in GATEWAY_PREPAID_PROVIDER_SLUGS) + a BYOK endpoint,
# synthesized by `_build_endpoints` exactly like a chat model, because
# `prepaid_available`/`byok_available` are set.
#
# PRICES are the providers' published per-million input rates as of
# 2026-06-07 (markup + $0.01/M floor applied via `_priced`); the
# pricing-refresh job should true them up. supports_chat=False keeps chat
# routing from ever selecting an embedding model; supports_embeddings=True
# is what `/embeddings/models` and the embeddings route filter on.




def _embedding_models() -> dict[str, Model]:
    """Seed the embedding-model catalog (input-only pricing)."""
    models: dict[str, Model] = {}
    for spec in _EMBEDDING_SPECS:
        if spec["provider"] not in PROVIDERS:
            continue
        prompt_price, published_price, _cost = _priced(spec["cost_dollars_per_million"])
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


_EMBEDDING_MODELS = _embedding_models()


_INGESTED_MODELS, _INGESTED_ENDPOINTS = _ingested_models_and_endpoints()
_SUPPLEMENTAL_MODELS, _SUPPLEMENTAL_ENDPOINTS = _supplemental_provider_models_and_endpoints()
# The OpenRouter ingest snapshot is the primary catalog. Provider-native
# supplements add exact routes from providers whose live model API is
# ahead of OpenRouter's endpoint feed. Pricing across both paths goes
# through the same `cost × 1.10, $0.01/M floor` formula.
MODELS.update(_INGESTED_MODELS)
for _model_id, _model in _SUPPLEMENTAL_MODELS.items():
    MODELS.setdefault(_model_id, _model)
# Embedding models override any snapshot/supplemental collision: the
# hand-curated embedding entry (input-only pricing, supports_embeddings) is
# authoritative for these IDs. Merge BEFORE `_build_endpoints` so each gets
# its Credits + BYOK endpoints synthesized.
for _model_id, _model in _EMBEDDING_MODELS.items():
    MODELS[_model_id] = _model

MODEL_ENDPOINTS: dict[str, ModelEndpoint] = _build_endpoints(MODELS)
MODEL_ENDPOINTS.update(_INGESTED_ENDPOINTS)
MODEL_ENDPOINTS.update(_SUPPLEMENTAL_ENDPOINTS)

# --- Provider served-model allowlist -------------------------------------
# Our upstream accounts don't always match OpenRouter's provider→model map.
# Routing a model a provider doesn't actually host on our account returns an
# upstream error (the gateway surfaces it as a 502). When an allowlist is set
# for a provider, ONLY its listed models keep that provider's endpoints; routes
# for any other model on that provider are dropped before serving/routing.
#
# Cerebras (the key wired into the enclave) serves only gpt-oss-120b and
# glm-4.7 on our account — verified 2026-06-04 from the Cerebras dashboard —
# NOT the Llama models OpenRouter lists for Cerebras's GA tier. Without this
# filter every Llama-via-Cerebras route 502s, and because Cerebras is rank-0
# ("fastest") it gets tried first for those models. The provider-native
# Cerebras manifest publishes the two verified canonical routes plus
# cerebras/* convenience aliases that map to the same upstream IDs.

# Inverse of the allowlist, but keyed by MODEL across ALL providers: specific
# prepaid (Credits) model ids that 502 on every provider that lists them, while
# every other model is kept. Use this for dead-everywhere models (an allowlist
# would force us to enumerate each provider's whole working set instead).
#
# OpenAI's GPT-5.4 line and the "-pro" tiers are closed models OpenAI does not
# serve on our key — verified 2026-06-04 via the gateway probe pinned to openai
# (gpt-5.5 => OK; gpt-5.4 / gpt-5.4-nano / gpt-5.4-pro / gpt-5.5-pro => 502).
# Because they are closed, no third-party prepaid host can serve them either:
# the snapshot's gmi endpoint for gpt-5.4-nano 502s too (verified). So drop
# their Credits routes on EVERY provider. (gpt-5.5 works and stays; BYOK routes
# are left intact as the customer's own responsibility.)

# Provider-keyed denylist: specific (provider, model) prepaid routes the
# OpenRouter snapshot lists but the provider's live API doesn't actually serve
# on our account — every one verified 502 pinned to that provider via the
# gateway probe, then cross-checked against the provider's own /models feed,
# 2026-06-04. Drop ONLY that provider's Credits route; the model still serves
# fine wherever it's real (its native provider and/or other hosts). Unlike the
# all-provider _UNSERVED_CREDITS_MODELS set, this is per provider, so a model
# that's dead on one host but healthy elsewhere keeps its working routes.
#
#   gmi      — open-weights GPU host; can't run the two closed models the
#              snapshot lists for it (anthropic/claude-opus-4.7, openai/gpt-5.5),
#              both of which serve fine on their native provider.
#   deepseek — DeepSeek-direct serves only deepseek-v4-flash/-v4-pro (its real
#              /models); the snapshot's chat-v3.1 and v3.2 routes 502.
#   nebius   — retired two older models still in the snapshot (gemma-2-2b-it,
#              Meta-Llama-3.1-8B-Instruct); its current /models has neither.
#   zai      — does not serve glm-4-32b or glm-4.7-flash (both absent from its
#              /models). NB: zai's glm-4.7 ALSO 502'd, but that was an ENCLAVE
#              model-id-map bug (zai serves glm-4.7 under the BARE id; the
#              enclave was sending "zai-glm-4.7") — fixed in quill-cloud-proxy
#              (zaiModelMap), so glm-4.7 is deliberately NOT dropped here.
#   gemini   — Google's Gemini API (closed gemini-* models) does NOT serve the
#              open-weights Gemma models on our key: every google/gemma-* route
#              pinned to gemini 502s (upstream_4xx), verified 2026-06-04. Gemma
#              is hosted by the open-weights providers (deepinfra/novita/parasail/
#              gmi/lightning), which work. gemini was ranked first for these, so
#              DEFAULT routing for Gemma was 502ing — drop gemini's Gemma routes.


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


MODEL_ENDPOINTS = _filter_unserved_provider_endpoints(MODEL_ENDPOINTS)


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


class InvalidAutoModelOrder(ValueError):
    """Raised when TR_AUTO_MODEL_ORDER includes a router/orchestration model."""


def validate_auto_model_order(order: str | None = None) -> None:
    raw_ids = [
        item.strip()
        for item in (order.split(",") if order else DEFAULT_AUTO_MODEL_ORDER)
        if item.strip()
    ]
    meta_ids = [model_id for model_id in raw_ids if model_id in META_MODEL_IDS]
    if meta_ids:
        joined = ", ".join(meta_ids)
        raise InvalidAutoModelOrder(
            "TR_AUTO_MODEL_ORDER cannot include TrustedRouter meta or orchestration "
            f"models: {joined}. Use regular provider/model IDs only."
        )


def auto_candidate_models(order: str | None = None) -> list[Model]:
    raw_ids = [
        item.strip()
        for item in (order.split(",") if order else DEFAULT_AUTO_MODEL_ORDER)
        if item.strip()
    ]
    validate_auto_model_order(order)
    candidates: list[Model] = []
    seen: set[str] = set()
    for model_id in raw_ids:
        if model_id in seen:
            continue
        model = MODELS.get(model_id)
        if model is not None and _is_regular_chat_model(model):
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


def fast_candidate_models(limit: int = 8) -> list[Model]:
    # Low-latency pool: Cerebras first, then Xiaomi MiMo's UltraSpeed tier.
    # Keep this as a small explicit pool so callers who choose
    # `trustedrouter/fast` do not accidentally get a cheap-but-slower model
    # just because it has a lower token price.
    preferred_ids = [
        "cerebras/gpt-oss-120b",
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "xiaomi/mimo-v2-flash",
        "cerebras/zai-glm-4.7",
    ]
    candidates: list[Model] = []
    seen: set[str] = set()
    for model_id in preferred_ids:
        model = MODELS.get(model_id)
        if model is not None and _is_regular_chat_model(model):
            candidates.append(model)
            seen.add(model.id)
        if len(candidates) >= limit:
            return candidates
    return candidates


def monitor_candidate_models(limit: int = 12) -> list[Model]:
    # Order favors models that reliably emit a visible one-token PONG.
    # DeepSeek V4 Flash is cheaper, but in thinking-default mode it can spend
    # the entire tiny output budget on hidden reasoning and return an empty
    # visible message. That is a false status-page outage. Keep cheap reasoning
    # models in the tail; use visible-output models for the steady-state probe.
    #
    # Costs at 2026-06 prices ($/M tokens, in / out):
    #   deepseek/deepseek-v4-flash    0.154 / 0.308   ← lead (4 providers)
    #   deepseek/deepseek-v3.2        0.308 / 0.495   ← same-family backup
    #   deepseek/deepseek-v4-pro      0.478 / 0.957   ← +tinfoil +gmi
    #   mistralai/mistral-small-2603  0.165 / 0.660   ← cross-provider
    #   openai/gpt-4.1-mini           0.440 / 1.760
    #   z-ai/glm-4.5-air              0.220 / 1.210
    #   google/gemini-2.5-flash       0.330 / 2.750
    #   z-ai/glm-4.6                  0.660 / 2.420   ← reasoning, tail
    #   moonshotai/kimi-k2.6          0.880 / 3.850   ← reasoning, tail
    #   anthropic/claude-haiku-4.5    1.100 / 5.500   ← most expensive
    preferred_ids = [
        "openai/gpt-4.1-mini",
        "mistralai/mistral-small-2603",
        "google/gemini-2.5-flash",
        "anthropic/claude-haiku-4.5",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v3.2",
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-4.5-air",
        "z-ai/glm-4.6",
        "moonshotai/kimi-k2.6",
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


def _privacy_candidate_models(
    *,
    min_tier: int,
    preferred_providers: tuple[str, ...] = (),
    allowed_providers: frozenset[str] | None = None,
    limit: int = 12,
) -> list[Model]:
    """Unique chat models with at least one endpoint clearing min_tier.

    This builds the model-level rollover ladder. The routing layer forces the
    same privacy floor, so the gateway still picks only qualifying endpoints.
    """
    provider_rank = {provider: index for index, provider in enumerate(preferred_providers)}
    eligible: list[tuple[int, int, int, str, Model]] = []
    per_provider: dict[str, list[tuple[int, int, Model]]] = {}
    for endpoint in MODEL_ENDPOINTS.values():
        provider = PROVIDERS.get(endpoint.provider)
        model = MODELS.get(endpoint.model_id)
        if (
            provider is None
            or model is None
            or not _is_regular_chat_model(model)
            or model.id.endswith(":free")
            or endpoint_privacy_tier(endpoint) < min_tier
        ):
            continue
        if allowed_providers is not None and endpoint.provider not in allowed_providers:
            continue
        price = (
            endpoint.prompt_price_microdollars_per_million_tokens
            + endpoint.completion_price_microdollars_per_million_tokens
        )
        usage_rank = 0 if endpoint.usage_type == "Credits" else 1
        rank = provider_rank.get(endpoint.provider, len(provider_rank))
        eligible.append((rank, usage_rank, price, endpoint.provider, model))
        per_provider.setdefault(endpoint.provider, []).append((usage_rank, price, model))

    result: list[Model] = []
    seen: set[str] = set()
    for provider_slug in preferred_providers:
        options = sorted(
            per_provider.get(provider_slug, []),
            key=lambda item: (item[0], item[1], item[2].provider, item[2].id),
        )
        for _usage_rank, _price, model in options:
            if model.id not in seen:
                result.append(model)
                seen.add(model.id)
                break
        if len(result) >= limit:
            return result

    for _rank, _usage_rank, _price, _provider, model in sorted(
        eligible,
        key=lambda item: (item[0], item[1], item[2], item[3], item[4].provider, item[4].id),
    ):
        if model.id in seen:
            continue
        result.append(model)
        seen.add(model.id)
        if len(result) >= limit:
            break
    return result


def eu_candidate_models(limit: int = 12) -> list[Model]:
    return _privacy_candidate_models(
        min_tier=PRIVACY_TIER_STANDARD,
        preferred_providers=EU_FOCUSED_PROVIDER_ORDER,
        allowed_providers=frozenset(EU_FOCUSED_PROVIDER_ORDER),
        limit=limit,
    )


def zdr_candidate_models(limit: int = 12) -> list[Model]:
    return _privacy_candidate_models(
        min_tier=PRIVACY_TIER_ZERO_RETENTION,
        preferred_providers=("anthropic", "openai", "gemini", "tinfoil", "venice", "phala"),
        limit=limit,
    )


def e2e_candidate_models(limit: int = 12) -> list[Model]:
    return _privacy_candidate_models(
        min_tier=PRIVACY_TIER_CONFIDENTIAL,
        preferred_providers=("tinfoil", "venice", "phala", "gmi"),
        limit=limit,
    )


def _models_for_ids(model_ids: tuple[str, ...]) -> list[Model]:
    models: list[Model] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id in seen or model_id not in MODELS:
            continue
        seen.add(model_id)
        models.append(MODELS[model_id])
    return models


def socrates_candidate_models() -> list[Model]:
    return _models_for_ids(SOCRATES_CATALOG_MODEL_ORDER)


def meta_candidate_models(model_id: str) -> list[Model]:
    if model_id == AUTO_MODEL_ID:
        return auto_candidate_models()
    if model_id == FREE_MODEL_ID:
        return free_candidate_models()
    if model_id == CHEAP_MODEL_ID:
        return cheap_candidate_models()
    if model_id == FAST_MODEL_ID:
        return fast_candidate_models()
    if model_id == EU_MODEL_ID:
        return eu_candidate_models()
    if model_id == ZDR_MODEL_ID:
        return zdr_candidate_models()
    if model_id == E2E_MODEL_ID:
        return e2e_candidate_models()
    if model_id == MONITOR_MODEL_ID:
        return monitor_candidate_models()
    advisor_order = ADVISOR_CATALOG_MODEL_ORDERS.get(model_id)
    if advisor_order is not None:
        return _models_for_ids(advisor_order)
    if model_id == PROMETHEUS_1_0_1M_MODEL_ID:
        return _models_for_ids(SYNTH_QUALITY_1M_MODEL_ORDER)
    if model_id in (PROMETHEUS_MODEL_ID, PROMETHEUS_1_0_MODEL_ID):
        return _models_for_ids(SYNTH_QUALITY_MODEL_ORDER)
    if model_id in (IRIS_MODEL_ID, IRIS_1_0_MODEL_ID):
        return _models_for_ids(SYNTH_BUDGET_MODEL_ORDER)
    if model_id in (ZEUS_MODEL_ID, ZEUS_1_0_MODEL_ID):
        return _models_for_ids(SYNTH_FRONTIER_MODEL_ORDER)
    if model_id == ZEUS_1_0_MINI_MODEL_ID:
        return _models_for_ids(SYNTH_FRONTIER_MINI_MODEL_ORDER)
    if model_id == OPEN_PATCHER_S1_MODEL_ID:
        return _models_for_ids(
            (
                "moonshotai/kimi-k2.7-code",
                "z-ai/glm-5.2",
            )
        )
    if model_id in (
        PROMETHEUS_CODE_MODEL_ID,
        PROMETHEUS_CODE_1_0_MODEL_ID,
    ):
        return _models_for_ids(SYNTH_CODE_QUALITY_MODEL_ORDER)
    if model_id in (IRIS_CODE_MODEL_ID, IRIS_CODE_1_0_MODEL_ID):
        return _models_for_ids(SYNTH_CODE_BUDGET_MODEL_ORDER)
    if model_id in (ZEUS_CODE_MODEL_ID, ZEUS_CODE_1_0_MODEL_ID):
        return _models_for_ids(SYNTH_CODE_FRONTIER_MODEL_ORDER)
    if model_id == SELECTOR_MODEL_ID:
        return _models_for_ids(SELECTOR_CATALOG_MODEL_ORDER)
    if model_id == MAPREDUCE_MODEL_ID:
        return _models_for_ids(MAPREDUCE_CATALOG_MODEL_ORDER)
    return []


def _meta_route_kind(model_id: str) -> str:
    if model_id == FREE_MODEL_ID:
        return "free_pool"
    if model_id == CHEAP_MODEL_ID:
        return "cheap_pool"
    if model_id == FAST_MODEL_ID:
        return "fast_pool"
    if model_id == EU_MODEL_ID:
        return "eu_pool"
    if model_id == ZDR_MODEL_ID:
        return "zdr_pool"
    if model_id == E2E_MODEL_ID:
        return "e2e_pool"
    if model_id == MONITOR_MODEL_ID:
        return "synthetic_monitor_pool"
    if model_id == SUBAGENT_MODEL_ID:
        return "subagent_orchestration"
    if model_id in ADVISOR_CATALOG_MODEL_ORDERS:
        return "advisor_orchestration"
    if model_id in (
        SYNTH_MODEL_ID,
        IRIS_MODEL_ID,
        PROMETHEUS_MODEL_ID,
        ZEUS_MODEL_ID,
        IRIS_1_0_MODEL_ID,
        PROMETHEUS_1_0_MODEL_ID,
        PROMETHEUS_1_0_1M_MODEL_ID,
        ZEUS_1_0_MODEL_ID,
        ZEUS_1_0_MINI_MODEL_ID,
        SYNTH_CODE_MODEL_ID,
        IRIS_CODE_MODEL_ID,
        PROMETHEUS_CODE_MODEL_ID,
        ZEUS_CODE_MODEL_ID,
        IRIS_CODE_1_0_MODEL_ID,
        PROMETHEUS_CODE_1_0_MODEL_ID,
        ZEUS_CODE_1_0_MODEL_ID,
        OPEN_PATCHER_S1_MODEL_ID,
        FUSION_MODEL_ID,
        FUSION_CODE_MODEL_ID,
    ):
        return "fusion_panel"
    if model_id == SELECTOR_MODEL_ID:
        return "selector_orchestration"
    if model_id == MAPREDUCE_MODEL_ID:
        return "mapreduce_orchestration"
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
    "deepseek/",
    "google/gemma",
    "meta-llama/",
    "minimax/minimax-m3",
    "moonshotai/kimi",
    "qwen/",
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
