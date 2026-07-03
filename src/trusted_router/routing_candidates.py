"""Candidate-model selection for TrustedRouter's meta-models and routing pools.

Given a meta-model target (auto / free / cheap / fast / monitor / eu / zdr /
e2e / socrates / synth / the meta router), these functions produce the ordered
list of concrete provider Models the attested gateway will try. This is the
money-path routing surface, split out of the catalog.py god-module (#38).

Depends on the catalog_registry (the built MODELS / MODEL_ENDPOINTS), the
catalog_data constants, and catalog_privacy.endpoint_privacy_tier — all leaves
with respect to catalog.py, so there is no import cycle."""

from __future__ import annotations

from trusted_router.catalog_data import (
    ADVISOR_CATALOG_MODEL_ORDERS,
    AUTO_MODEL_ID,
    CHEAP_MODEL_ID,
    DEFAULT_AUTO_MODEL_ORDER,
    E2E_MODEL_ID,
    EU_FOCUSED_PROVIDER_ORDER,
    EU_MODEL_ID,
    FAST_MODEL_ID,
    FREE_MODEL_ID,
    FUSION_CODE_MODEL_ID,
    FUSION_MODEL_ID,
    IRIS_1_0_MODEL_ID,
    IRIS_CODE_1_0_MODEL_ID,
    IRIS_CODE_MODEL_ID,
    IRIS_MODEL_ID,
    MAPREDUCE_CATALOG_MODEL_ORDER,
    MAPREDUCE_MODEL_ID,
    META_MODEL_IDS,
    MONITOR_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROMETHEUS_1_0_1M_MODEL_ID,
    PROMETHEUS_1_0_MODEL_ID,
    PROMETHEUS_CODE_1_0_MODEL_ID,
    PROMETHEUS_CODE_MODEL_ID,
    PROMETHEUS_MODEL_ID,
    PROVIDERS,
    SELECTOR_CATALOG_MODEL_ORDER,
    SELECTOR_MODEL_ID,
    SOCRATES_CATALOG_MODEL_ORDER,
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
    ZDR_MODEL_ID,
    ZEUS_1_0_MINI_MODEL_ID,
    ZEUS_1_0_MODEL_ID,
    ZEUS_CODE_1_0_MODEL_ID,
    ZEUS_CODE_MODEL_ID,
    ZEUS_MODEL_ID,
    Model,
)
from trusted_router.catalog_privacy import endpoint_privacy_tier
from trusted_router.catalog_registry import MODEL_ENDPOINTS, MODELS


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
