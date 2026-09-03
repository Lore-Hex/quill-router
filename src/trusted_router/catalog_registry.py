"""Model registry — builds the MODELS and MODEL_ENDPOINTS dicts at import time.

This is the money-critical construction of TR's catalog: the hand-coded Auto
seed plus every model/endpoint ingested from data/openrouter_snapshot.json and
the supplemental provider manifests, priced uniformly (cost x1.055, $0.01/M
floor). Split out of catalog.py so the ~575-line import-time build lives on its
own; catalog.py re-exports MODELS/MODEL_ENDPOINTS and layers the privacy /
routing / serialization query helpers on top. No catalog.py functions are used
here (verified) so there is no import cycle: this module depends only on the
catalog_data / catalog_ingest / pricing leaves."""

from __future__ import annotations

import json
from dataclasses import replace

from trusted_router.catalog_data import (  # noqa: F401 - re-exported for back-compat
    _EMBEDDING_SPECS,
    _MODEL_PROVIDER_PRIVACY_OVERRIDES,
    _PROVIDER_DISPLAY_ORDER,
    _PROVIDER_SERVED_MODEL_ALLOWLIST,
    _PROVIDER_UNSERVED_CREDITS_MODELS,
    _UNSERVED_CREDITS_MODELS,
    ADVISOR_CATALOG_MODEL_ORDERS,
    ADVISOR_MODEL_ID,
    ARCHIMEDES_1_0_MODEL_ID,
    ARISTOTLE_1_0_MODEL_ID,
    ARISTOTLE_1_1_MODEL_ID,
    ARISTOTLE_2_0_MODEL_ID,
    ARISTOTLE_MODEL_ID,
    ATHENA_1_0_MODEL_ID,
    ATHENA_2_0_MODEL_ID,
    ATHENA_MODEL_ID,
    AUTO_MODEL_ID,
    CANONICAL_ORCHESTRATION_MODEL_ID,
    CHEAP_MODEL_ID,
    CONFIDENTIAL_MODEL_ID,
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
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
    IRIS_2_0_MODEL_ID,
    IRIS_3_0_MODEL_ID,
    IRIS_CODE_1_0_MODEL_ID,
    IRIS_CODE_MODEL_ID,
    IRIS_MODEL_ID,
    LIBERTY_1_0_1M_MODEL_ID,
    LIBERTY_1_0_MODEL_ID,
    LIBERTY_2_0_MODEL_ID,
    LIBERTY_3_0_MODEL_ID,
    MAPREDUCE_CATALOG_MODEL_ORDER,
    MAPREDUCE_MODEL_ID,
    META_MODEL_IDS,
    MISTRAL_LARGE_MODEL_ID,
    MONITOR_MODEL_ID,
    OPEN_PATCHER_A1_MODEL_ID,
    OPEN_PATCHER_FAST1_MODEL_ID,
    OPEN_PATCHER_G1_MODEL_ID,
    OPEN_PATCHER_G2_MODEL_ID,
    OPEN_PATCHER_G3_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    OPEN_PATCHER_S2_MODEL_ID,
    OPEN_PATCHER_S3_MODEL_ID,
    ORCHESTRATION_LEGACY_ALIAS_MODEL_IDS,
    ORCHESTRATION_PRIMITIVE_BY_MODEL_ID,
    ORCHESTRATION_PRIMITIVE_MODEL_IDS,
    ORCHESTRATION_PRIMITIVE_NAMES,
    ORCHESTRATION_ROLLING_ALIAS_MODEL_IDS,
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    PLATO_1_0_MODEL_ID,
    PLATO_3_0_MODEL_ID,
    PLATO_MODEL_ID,
    PLATO_PRO_1_0_MODEL_ID,
    PLATO_PRO_2_0_MODEL_ID,
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
    PROMETHEUS_3_0_MODEL_ID,
    PROMETHEUS_CODE_1_0_MODEL_ID,
    PROMETHEUS_CODE_MODEL_ID,
    PROMETHEUS_MODEL_ID,
    PROVIDER_JURISDICTION_US,
    PROVIDERS,
    SELECTOR_CATALOG_MODEL_ORDER,
    SELECTOR_MODEL_ID,
    SOCRATES_1_0_MODEL_ID,
    SOCRATES_1_1_MODEL_ID,
    SOCRATES_2_0_MODEL_ID,
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
    SYNTH_CODE_BUDGET_1_MODEL_ORDER,
    SYNTH_CODE_BUDGET_MODEL_ORDER,
    SYNTH_CODE_FRONTIER_MODEL_ORDER,
    SYNTH_CODE_MODEL_ID,
    SYNTH_CODE_QUALITY_1_MODEL_ORDER,
    SYNTH_CODE_QUALITY_MODEL_ORDER,
    SYNTH_FRONTIER_1_MODEL_ORDER,
    SYNTH_FRONTIER_MINI_MODEL_ORDER,
    SYNTH_FRONTIER_MODEL_ORDER,
    SYNTH_IRIS_1_MODEL_ORDER,
    SYNTH_IRIS_2_MODEL_ORDER,
    SYNTH_IRIS_3_MODEL_ORDER,
    SYNTH_MODEL_ID,
    SYNTH_PROMETHEUS_1_MODEL_ORDER,
    SYNTH_PROMETHEUS_2_MODEL_ORDER,
    SYNTH_PROMETHEUS_3_MODEL_ORDER,
    SYNTH_QUALITY_1M_MODEL_ORDER,
    SYNTH_QUALITY_MODEL_ORDER,
    US_PROVIDER_ONLY_MODEL_IDS,
    ZDR_MODEL_ID,
    ZEUS_1_0_MINI_MODEL_ID,
    ZEUS_1_0_MODEL_ID,
    ZEUS_2_0_MODEL_ID,
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
    _apply_provider_manifest_expiry,
    _author_provider,
    _build_endpoints,
    _embedding_models,
    _endpoint,
    _filter_unserved_provider_endpoints,
    _ingested_models_and_endpoints,
    _is_provider_deprecated_model,
    _supplemental_provider_models_and_endpoints,
)
from trusted_router.partner_billing import (
    PARASAIL_LIBERTY_2_0_INPUT_MICRODOLLARS_PER_MILLION,
    PARASAIL_LIBERTY_2_0_MINIMUM_CHARGE_MICRODOLLARS,
    PARASAIL_LIBERTY_2_0_OUTPUT_MICRODOLLARS_PER_MILLION,
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

# Catalog seed — only TR's Auto meta-model is hand-coded. Every other
# entry comes from `_INGESTED_MODELS` below, which is built from
# `data/openrouter_snapshot.json`. That guarantees pricing is uniformly
# `cost × 1.055, $0.01/M floor` (per the formula), and that the catalog
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
    CONFIDENTIAL_MODEL_ID: Model(
        id=CONFIDENTIAL_MODEL_ID,
        name="TrustedRouter Confidential",
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
    SOCRATES_2_0_MODEL_ID: Model(
        id=SOCRATES_2_0_MODEL_ID,
        name="TrustedRouter Socrates 2.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
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
    ARISTOTLE_1_1_MODEL_ID: Model(
        id=ARISTOTLE_1_1_MODEL_ID,
        name="TrustedRouter Aristotle 1.1",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ARISTOTLE_2_0_MODEL_ID: Model(
        id=ARISTOTLE_2_0_MODEL_ID,
        name="TrustedRouter Aristotle 2.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    ARISTOTLE_MODEL_ID: Model(
        id=ARISTOTLE_MODEL_ID,
        name="TrustedRouter Aristotle",
        provider="trustedrouter",
        context_length=1_048_576,
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
    PLATO_3_0_MODEL_ID: Model(
        id=PLATO_3_0_MODEL_ID,
        name="TrustedRouter Plato 3.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
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
    PLATO_PRO_2_0_MODEL_ID: Model(
        id=PLATO_PRO_2_0_MODEL_ID,
        name="TrustedRouter Plato Pro 2.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
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
    OPEN_PATCHER_S2_MODEL_ID: Model(
        id=OPEN_PATCHER_S2_MODEL_ID,
        name="TrustedRouter OpenPatcher-S2",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    OPEN_PATCHER_S3_MODEL_ID: Model(
        id=OPEN_PATCHER_S3_MODEL_ID,
        name="TrustedRouter OpenPatcher-S3",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
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
    OPEN_PATCHER_G2_MODEL_ID: Model(
        id=OPEN_PATCHER_G2_MODEL_ID,
        name="TrustedRouter OpenPatcher-G2",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    OPEN_PATCHER_G3_MODEL_ID: Model(
        id=OPEN_PATCHER_G3_MODEL_ID,
        name="TrustedRouter OpenPatcher-G3",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    ATHENA_1_0_MODEL_ID: Model(
        id=ATHENA_1_0_MODEL_ID,
        name="TrustedRouter Athena 1.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
        hidden_public_metadata=True,
    ),
    ATHENA_2_0_MODEL_ID: Model(
        id=ATHENA_2_0_MODEL_ID,
        name="TrustedRouter Athena 2.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
        hidden_public_metadata=True,
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
    LIBERTY_1_0_MODEL_ID: Model(
        id=LIBERTY_1_0_MODEL_ID,
        name="TrustedRouter Liberty 1.0",
        provider="trustedrouter",
        context_length=262_144,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    LIBERTY_1_0_1M_MODEL_ID: Model(
        id=LIBERTY_1_0_1M_MODEL_ID,
        name="TrustedRouter Liberty 1.0 1M",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    LIBERTY_2_0_MODEL_ID: Model(
        id=LIBERTY_2_0_MODEL_ID,
        name="TrustedRouter Liberty 2.0",
        provider="trustedrouter",
        context_length=262_144,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PARASAIL_LIBERTY_2_0_MODEL_ID: Model(
        id=PARASAIL_LIBERTY_2_0_MODEL_ID,
        name="Parasail Liberty 2.0",
        provider="parasail",
        context_length=262_144,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
        prompt_price_microdollars_per_million_tokens=(
            PARASAIL_LIBERTY_2_0_INPUT_MICRODOLLARS_PER_MILLION
        ),
        completion_price_microdollars_per_million_tokens=(
            PARASAIL_LIBERTY_2_0_OUTPUT_MICRODOLLARS_PER_MILLION
        ),
        published_prompt_price_microdollars_per_million_tokens=(
            PARASAIL_LIBERTY_2_0_INPUT_MICRODOLLARS_PER_MILLION
        ),
        published_completion_price_microdollars_per_million_tokens=(
            PARASAIL_LIBERTY_2_0_OUTPUT_MICRODOLLARS_PER_MILLION
        ),
        minimum_charge_microdollars=(PARASAIL_LIBERTY_2_0_MINIMUM_CHARGE_MICRODOLLARS),
    ),
    LIBERTY_3_0_MODEL_ID: Model(
        id=LIBERTY_3_0_MODEL_ID,
        name="TrustedRouter Liberty 3.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
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
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    PROMETHEUS_MODEL_ID: Model(
        id=PROMETHEUS_MODEL_ID,
        name="TrustedRouter Prometheus",
        provider="trustedrouter",
        context_length=1_048_576,
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
    IRIS_2_0_MODEL_ID: Model(
        id=IRIS_2_0_MODEL_ID,
        name="TrustedRouter Iris 2.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    IRIS_3_0_MODEL_ID: Model(
        id=IRIS_3_0_MODEL_ID,
        name="TrustedRouter Iris 3.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
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
    PROMETHEUS_2_0_MODEL_ID: Model(
        id=PROMETHEUS_2_0_MODEL_ID,
        name="TrustedRouter Prometheus 2.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    PROMETHEUS_3_0_MODEL_ID: Model(
        id=PROMETHEUS_3_0_MODEL_ID,
        name="TrustedRouter Prometheus 3.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
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
    ZEUS_2_0_MODEL_ID: Model(
        id=ZEUS_2_0_MODEL_ID,
        name="TrustedRouter Zeus 2.0",
        provider="trustedrouter",
        context_length=1_048_576,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
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


# Folder where the OpenRouter ingest snapshot lives. Bundled into the
# wheel so production reads from disk; refreshed by
# `scripts/ingest_openrouter_catalog.py` and committed via PR.

# OpenRouter publishes models as `{author}/{slug}` where author maps onto
# one of TR's keyed providers. This drops the `Model.provider` (publisher)
# field for an ingested entry.


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


_EMBEDDING_MODELS = _embedding_models()


_INGESTED_MODELS, _INGESTED_ENDPOINTS = _ingested_models_and_endpoints()
_SUPPLEMENTAL_MODELS, _SUPPLEMENTAL_ENDPOINTS = _supplemental_provider_models_and_endpoints()
# The OpenRouter ingest snapshot is the primary catalog. Provider-native
# supplements add exact routes from providers whose live model API is
# ahead of OpenRouter's endpoint feed. Pricing across both paths goes
# through the same `cost × 1.055, $0.01/M floor` formula.
MODELS.update(_INGESTED_MODELS)
for _model_id, _model in _SUPPLEMENTAL_MODELS.items():
    _existing_model = MODELS.get(_model_id)
    if _existing_model is None:
        MODELS[_model_id] = _model
        continue
    # A provider-native catalog may advertise a capability before the shared
    # snapshot does. Preserve the primary catalog's pricing/name while taking
    # the union of capabilities verified by callable provider routes.
    MODELS[_model_id] = replace(
        _existing_model,
        input_modalities=tuple(
            dict.fromkeys((*_existing_model.input_modalities, *_model.input_modalities))
        ),
        output_modalities=tuple(
            dict.fromkeys((*_existing_model.output_modalities, *_model.output_modalities))
        ),
    )

# Archimedes is a one-model private proxy. Clone the canonical model after
# native manifests have merged so context, capabilities, cache pricing, and
# future price refreshes remain byte-for-byte aligned with Mistral Large.
_archimedes_backing_model = MODELS[MISTRAL_LARGE_MODEL_ID]
MODELS[ARCHIMEDES_1_0_MODEL_ID] = replace(
    _archimedes_backing_model,
    id=ARCHIMEDES_1_0_MODEL_ID,
    name="TrustedRouter Archimedes 1.0",
    provider="trustedrouter",
    upstream_id=None,
    prepaid_available=True,
    byok_available=False,
    hidden_public_metadata=True,
)
# Embedding models override any snapshot/supplemental collision: the
# hand-curated embedding entry (input-only pricing, supports_embeddings) is
# authoritative for these IDs. Merge BEFORE `_build_endpoints` so each gets
# its Credits + BYOK endpoints synthesized.
for _model_id, _model in _EMBEDDING_MODELS.items():
    MODELS[_model_id] = _model

# Video generation is a separate asynchronous product surface. These models
# are deliberately not inferred from the text-model snapshot: each entry is
# backed by a provider-native video queue that the attested gateway knows how
# to quote, submit, poll, and clean up. Token prices are zero because billing
# uses the provider's per-job quote as a fixed integer-microdollar charge.
_VIDEO_MODELS: dict[str, Model] = {
    "bytedance/seedance-2.0": Model(
        id="bytedance/seedance-2.0",
        name="ByteDance Seedance 2.0",
        provider="venice",
        context_length=10_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image", "audio", "video"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "bytedance/seedance-2.0-fast": Model(
        id="bytedance/seedance-2.0-fast",
        name="ByteDance Seedance 2.0 Fast",
        provider="venice",
        context_length=10_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image", "audio", "video"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "lightricks/ltx-2.3": Model(
        id="lightricks/ltx-2.3",
        name="Lightricks LTX 2.3",
        provider="ltx",
        context_length=5_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "lightricks/ltx-2.3-fast": Model(
        id="lightricks/ltx-2.3-fast",
        name="Lightricks LTX 2.3 Fast",
        provider="ltx",
        context_length=5_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "google/gemini-omni-flash": Model(
        id="google/gemini-omni-flash",
        name="Google Gemini Omni Flash",
        provider="venice",
        context_length=1_048_576,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "minimax/hailuo-3": Model(
        id="minimax/hailuo-3",
        name="MiniMax Hailuo 3 (H3)",
        provider="minimax",
        context_length=7_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image", "audio", "video"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "minimax/h3-max": Model(
        id="minimax/h3-max",
        name="MiniMax H3 Max",
        provider="fal",
        context_length=10_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "google/veo-3.1": Model(
        id="google/veo-3.1",
        name="Google Veo 3.1",
        provider="google-ai-studio",
        context_length=2_500,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "google/veo-3.1-fast": Model(
        id="google/veo-3.1-fast",
        name="Google Veo 3.1 Fast",
        provider="google-ai-studio",
        context_length=2_500,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "openai/sora-2": Model(
        id="openai/sora-2",
        name="OpenAI Sora 2",
        provider="openai",
        context_length=2_500,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "openai/sora-2-pro": Model(
        id="openai/sora-2-pro",
        name="OpenAI Sora 2 Pro",
        provider="openai",
        context_length=2_500,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "runway/gen-4.5": Model(
        id="runway/gen-4.5",
        name="Runway Gen-4.5",
        provider="runway",
        context_length=1_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "kling/v3-pro": Model(
        id="kling/v3-pro",
        name="Kling Video 3.0 Pro",
        provider="kling",
        context_length=3_072,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "kling/o3-pro": Model(
        id="kling/o3-pro",
        name="Kling Video 3.0 Omni Pro",
        provider="kling",
        context_length=3_072,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "alibaba/wan-2.7": Model(
        id="alibaba/wan-2.7",
        name="Alibaba Wan 2.7",
        provider="alibaba",
        context_length=5_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "x-ai/grok-imagine-video": Model(
        id="x-ai/grok-imagine-video",
        name="xAI Grok Imagine Video",
        provider="grok",
        context_length=10_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "shengshu/vidu-q3": Model(
        id="shengshu/vidu-q3",
        name="ShengShu Vidu Q3",
        provider="venice",
        context_length=2_500,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "pixverse/c1": Model(
        id="pixverse/c1",
        name="PixVerse C1",
        provider="venice",
        context_length=2_500,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "decart/lucy-2.5": Model(
        id="decart/lucy-2.5",
        name="Decart Lucy 2.5",
        provider="decart",
        context_length=10_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image", "video"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "decart/lucy-vton-3.5": Model(
        id="decart/lucy-vton-3.5",
        name="Decart Lucy VTON 3.5",
        provider="decart",
        context_length=10_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image", "video"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
    "decart/lucy-restyle-2": Model(
        id="decart/lucy-restyle-2",
        name="Decart Lucy Restyle 2",
        provider="decart",
        context_length=10_000,
        supports_chat=False,
        supports_video=True,
        input_modalities=("text", "image", "video"),
        output_modalities=("video",),
        prepaid_available=True,
        byok_available=False,
    ),
}
MODELS.update(_VIDEO_MODELS)

MODEL_ENDPOINTS: dict[str, ModelEndpoint] = _build_endpoints(MODELS)
MODEL_ENDPOINTS.update(_INGESTED_ENDPOINTS)
MODEL_ENDPOINTS.update(_SUPPLEMENTAL_ENDPOINTS)


def _install_deepseek_v4_pro_release_routes() -> None:
    """Install honest release-specific leaves for immutable combo presets.

    DeepSeek's API accepts only ``deepseek-v4-pro``. Its official model page
    identifies that rolling upstream as build 0813 as of 2026-08-13. Additional
    providers enter the immutable 0813 leaf only when their provider-native
    catalog exposes that exact release ID. The 0423 leaf is cloned only from
    snapshot endpoints explicitly labeled 20260423 and excludes the now rolling
    first-party route.
    """
    base = MODELS.get("deepseek/deepseek-v4-pro")
    if base is None:
        raise RuntimeError("DeepSeek V4 Pro base model is missing")

    snapshot = json.loads(_INGEST_PATH.read_text())
    historical_provider_slugs = {
        str(endpoint.get("tr_provider_slug") or "")
        for model in snapshot.get("models", [])
        if model.get("id") == base.id
        for endpoint in model.get("endpoints", [])
        if "20260423" in str(endpoint.get("name") or "")
        and endpoint.get("tr_provider_slug") != "deepseek"
    }
    historical = [
        endpoint
        for endpoint in _INGESTED_ENDPOINTS.values()
        if endpoint.model_id == base.id
        and endpoint.usage_type == "Credits"
        and endpoint.provider in historical_provider_slugs
    ]
    current = MODEL_ENDPOINTS.get(f"{base.id}@deepseek/prepaid")
    baseten_current = MODEL_ENDPOINTS.get(
        f"{DEEPSEEK_V4_PRO_0813_MODEL_ID}@baseten/prepaid"
    )
    fireworks_current = MODEL_ENDPOINTS.get(
        f"{DEEPSEEK_V4_PRO_0813_MODEL_ID}@fireworks/prepaid"
    )
    if (
        not historical
        or current is None
        or baseten_current is None
        or fireworks_current is None
    ):
        raise RuntimeError("DeepSeek V4 Pro release routes are incomplete")

    # Versioned release IDs are immutable Credits-only products. The hourly
    # provider manifests may discover matching native IDs later, but those
    # supplemental rows must not silently add BYOK routes or alter the route
    # set behind an already-published version.
    for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
        if endpoint.model_id in {
            DEEPSEEK_V4_PRO_0423_MODEL_ID,
            DEEPSEEK_V4_PRO_0813_MODEL_ID,
        }:
            del MODEL_ENDPOINTS[endpoint_id]

    def install(
        model_id: str,
        name: str,
        sources: list[ModelEndpoint],
    ) -> None:
        prompt = min(endpoint.prompt_price_microdollars_per_million_tokens for endpoint in sources)
        completion = min(
            endpoint.completion_price_microdollars_per_million_tokens for endpoint in sources
        )
        cheapest = min(
            sources,
            key=lambda endpoint: endpoint.prompt_price_microdollars_per_million_tokens,
        )
        MODELS[model_id] = replace(
            base,
            id=model_id,
            name=name,
            prepaid_available=True,
            byok_available=False,
            prompt_price_microdollars_per_million_tokens=prompt,
            completion_price_microdollars_per_million_tokens=completion,
            published_prompt_price_microdollars_per_million_tokens=prompt,
            published_completion_price_microdollars_per_million_tokens=completion,
            price_tiers=cheapest.price_tiers,
            published_price_tiers=cheapest.published_price_tiers,
        )
        for source in sources:
            endpoint_id = f"{model_id}@{source.provider}/prepaid"
            MODEL_ENDPOINTS[endpoint_id] = replace(
                source,
                id=endpoint_id,
                model_id=model_id,
            )

    install(
        DEEPSEEK_V4_PRO_0423_MODEL_ID,
        "DeepSeek V4 Pro 0423",
        historical,
    )
    install(
        DEEPSEEK_V4_PRO_0813_MODEL_ID,
        "DeepSeek V4 Pro 0813",
        [current, baseten_current, fireworks_current],
    )


_install_deepseek_v4_pro_release_routes()

_VIDEO_UPSTREAM_IDS = {
    "bytedance/seedance-2.0": "seedance-2-0-text-to-video",
    "bytedance/seedance-2.0-fast": "seedance-2-0-fast-text-to-video",
    "lightricks/ltx-2.3": "ltx-2-v2-3-full-text-to-video",
    "lightricks/ltx-2.3-fast": "ltx-2-v2-3-fast-text-to-video",
    "google/gemini-omni-flash": "gemini-omni-flash-text-to-video",
    "minimax/hailuo-3": "minimax-h3-text-to-video",
    "google/veo-3.1": "veo3.1-full-text-to-video",
    "google/veo-3.1-fast": "veo3.1-fast-text-to-video",
    "openai/sora-2": "sora-2-text-to-video",
    "openai/sora-2-pro": "sora-2-pro-text-to-video",
    "runway/gen-4.5": "runway-gen4-5-text",
    "kling/v3-pro": "kling-v3-pro-text-to-video",
    "kling/o3-pro": "kling-o3-pro-text-to-video",
    "alibaba/wan-2.7": "wan-2-7-text-to-video",
    "shengshu/vidu-q3": "vidu-q3-text-to-video",
    "pixverse/c1": "pixverse-c1-text-to-video",
}
_NATIVE_VIDEO_UPSTREAM_IDS = {
    "lightricks/ltx-2.3": (("ltx", "ltx-2-3-pro"),),
    "lightricks/ltx-2.3-fast": (("ltx", "ltx-2-3-fast"),),
    "minimax/hailuo-3": (
        ("atlas-cloud", "minimax/h3/text-to-video"),
    ),
    "minimax/h3-max": (("fal", "minimax/h3-max/text-to-video"),),
    "google/veo-3.1": (("google-ai-studio", "veo-3.1-generate-preview"),),
    "google/veo-3.1-fast": (("google-ai-studio", "veo-3.1-fast-generate-preview"),),
    "alibaba/wan-2.7": (("alibaba", "wan2.7-t2v"),),
    "x-ai/grok-imagine-video": (("grok", "grok-imagine-video"),),
    "runway/gen-4.5": (("runway", "gen4.5"),),
    "openai/sora-2": (("openai", "sora-2"),),
    "openai/sora-2-pro": (("openai", "sora-2-pro"),),
    "kling/v3-pro": (("kling", "kling-3.0"),),
    "kling/o3-pro": (("kling", "kling-3.0-omni"),),
    "decart/lucy-2.5": (("decart", "lucy-2.5"),),
    "decart/lucy-vton-3.5": (("decart", "lucy-vton-3.5"),),
    "decart/lucy-restyle-2": (("decart", "lucy-restyle-2"),),
}
for _model_id, _native_routes in _NATIVE_VIDEO_UPSTREAM_IDS.items():
    for _provider_slug, _upstream_id in _native_routes:
        _endpoint_id = f"{_model_id}@{_provider_slug}/prepaid"
        MODEL_ENDPOINTS[_endpoint_id] = ModelEndpoint(
            id=_endpoint_id,
            model_id=_model_id,
            provider=_provider_slug,
            usage_type="Credits",
            upstream_id=_upstream_id,
        )
for _model_id, _upstream_id in _VIDEO_UPSTREAM_IDS.items():
    _endpoint_id = f"{_model_id}@venice/prepaid"
    MODEL_ENDPOINTS[_endpoint_id] = ModelEndpoint(
        id=_endpoint_id,
        model_id=_model_id,
        provider="venice",
        usage_type="Credits",
        upstream_id=_upstream_id,
    )

# --- Provider served-model allowlist -------------------------------------
# Our upstream accounts don't always match OpenRouter's provider→model map.
# Routing a model a provider doesn't actually host on our account returns an
# upstream error (the gateway surfaces it as a 502). When an allowlist is set
# for a provider, ONLY its listed models keep that provider's endpoints; routes
# for any other model on that provider are dropped before serving/routing.
#
# Cerebras and Together use their generated provider manifests as the
# authoritative prepaid allowlist. That keeps OpenRouter inventory from
# reintroducing unavailable routes while allowing a newly discovered official
# serverless model to become routable without another source-code allowlist.

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


MODEL_ENDPOINTS = _apply_provider_manifest_expiry(MODEL_ENDPOINTS)
MODEL_ENDPOINTS = _filter_unserved_provider_endpoints(
    MODEL_ENDPOINTS,
    explicit_model_ids=frozenset(_VIDEO_MODELS),
)
