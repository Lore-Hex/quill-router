from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from trusted_router.catalog import (
    AUTO_MODEL_ID,
    E2E_MODEL_ID,
    EU_FOCUSED_PROVIDER_ORDER,
    EU_MODEL_ID,
    FUSION_CODE_MODEL_ID,
    FUSION_MODEL_ID,
    MAPREDUCE_MODEL_ID,
    META_MODEL_IDS,
    MODELS,
    PRIVACY_TIER_ALIASES,
    PRIVACY_TIER_NO_STORE,
    PROVIDER_JURISDICTION_US,
    PROVIDERS,
    ROUTING_MODEL_ALIAS_TARGETS,
    ROUTING_MODEL_MIN_PRIVACY_TIERS,
    SELECTOR_MODEL_ID,
    SYNTH_CODE_MODEL_ID,
    SYNTH_MODEL_ID,
    US_PROVIDER_ONLY_MODEL_IDS,
    ZDR_MODEL_ID,
    Model,
    ModelEndpoint,
    auto_candidate_models,
    endpoint_privacy_tier,
    endpoints_for_model,
    meta_candidate_models,
    model_max_privacy_tier,
)
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.types import ErrorType


@dataclass(frozen=True)
class RoutePreferences:
    order: tuple[str, ...] = ()
    only: frozenset[str] = frozenset()
    ignore: frozenset[str] = frozenset()
    allow_fallbacks: bool = True
    data_collection: str | None = None
    sort: str | None = None
    usage_type: str | None = None
    provider_jurisdiction: str | None = None
    # Minimum upstream-provider privacy tier (see catalog.PRIVACY_TIER_*).
    # 0 = no filter (default). Set via provider.min_privacy in the body.
    min_privacy_rank: int = 0


_PROVIDER_ALIASES = {
    "google-ai": "google-ai-studio",
    "ai-studio": "google-ai-studio",
    "google-vertex-ai": "google-vertex",
    "vertex": "google-vertex",
    "vertex-ai": "google-vertex",
    "chatgpt": "openai",
    "chat-gpt": "openai",
    "mistralai": "mistral",
    "mistral-ai": "mistral",
    "moonshot": "kimi",
    "moonshot-ai": "kimi",
    "kimi": "kimi",
    "z-ai": "zai",
    "zhipu": "zai",
    "zhipuai": "zai",
    "together-ai": "together",
    "togetherai": "together",
}
_PROVIDER_GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    # Before the provider split, both products were exposed as `gemini`.
    # Expand legacy filters to both real failure domains rather than silently
    # changing existing callers to just one of them.
    "gemini": ("google-vertex", "google-ai-studio"),
    "google": ("google-vertex", "google-ai-studio"),
}
_ROUTER_PROVIDER_SLUGS = frozenset(
    {
        "openrouter",
        "open-router",
        "trustedrouter",
        "trusted-router",
        "quillrouter",
        "quill-router",
    }
)

# OpenRouter-style model-id suffixes. Append `:nitro` to a model id to
# re-sort the upstream provider list by throughput-first (equivalent to
# setting `provider.sort = "throughput"` in the request body — see
# https://openrouter.ai/docs/guides/routing/model-variants/nitro). The
# table is intentionally extensible: adding `:floor` (price-first),
# `:thinking`, etc is a one-line edit. Each value pair is the
# RoutePreferences field name + the value to force.
_VARIANT_SUFFIXES: dict[str, tuple[str, str]] = {
    ":nitro": ("sort", "throughput"),
    ":floor": ("sort", "price"),
}


# Throughput-first routing rank. Lower values are tried first for
# `provider.sort = "throughput"` and `:nitro`.
#
# Generated from the public /leaderboard provider table on 2026-06-27 with:
#   python scripts/update_provider_throughput_rank.py --write
# The generator admits only providers with enough samples, >=95% measured uptime,
# and positive p50 output tokens/second. Providers without reliable token/s data
# keep conservative secondary ranks so they do not beat measured fast routes.
_THROUGHPUT_RANK = {
    "baseten": 0,
    "deepseek": 1,
    "fireworks": 2,
    "kimi": 3,
    "siliconflow": 4,
    "deepinfra": 5,
    "minimax": 6,
    "crusoe": 7,
    # Current leaderboard rows do not expose enough usable token/s for these
    # providers. Keep strong prior ordering below the measured set until the
    # synthetic probes emit stable longer completions for every provider.
    "cerebras": 20,
    "mistral": 21,
    "openai": 22,
    "google-vertex": 23,
    "google-ai-studio": 24,
    "together": 25,
    "zai": 26,
    "anthropic": 27,
    "tinfoil": 28,
    "venice": 29,
    "grok": 30,
    "lightning": 31,
    "nebius": 32,
    "friendli": 33,
    "novita": 34,
    "phala": 35,
    "gmi": 36,
    "parasail": 37,
    "wafer": 38,
    "xiaomi": 39,
    "trustedrouter": 99,
}

# Phase 4 — reliability-informed DEFAULT routing preference. Lower = tried
# first. When a model is served by several prepaid hosts, default traffic
# routes to the more RELIABLE host rather than raw catalog order. Demotions are
# evidence-based, from measured uptime on the public leaderboard (2026-06):
# gmi ~82% (and slow ~2.8s TTFT), parasail ~87%, novita ~94% — all materially
# below the reliable open-weight hosts (deepinfra / cerebras / lightning /
# deepseek ~100%). Everything unlisted stays at the reliable default; within a
# tier the catalog order is preserved, so only models served by BOTH a flaky
# and a healthy host change. Explicit `provider.order` and
# `sort=price|throughput` still take precedence. (A future per-model measured
# snapshot will refine this static floor — see Phase 4 plan.)
_DEFAULT_PROVIDER_PREFERENCE = 0
_PROVIDER_PREFERENCE = {
    "novita": 3,
    "parasail": 4,
    "gmi": 5,
}

# Narrow, evidence-backed exceptions to the global provider preference. These
# affect only default routing; caller-supplied provider.order/sort still wins.
_MODEL_PROVIDER_PREFERENCE: dict[str, dict[str, int]] = {
    "z-ai/glm-5.2": {"parasail": -1},
}

_CandidateT = TypeVar("_CandidateT")


def chat_route_candidates(body: dict[str, Any], settings: Settings) -> list[Model]:
    raw_ids, prefs = _routing_for_body(body, settings)
    candidates: list[Model] = []
    seen: set[str] = set()
    for model_id in raw_ids:
        model = MODELS.get(model_id)
        if model is None or not model.supports_chat:
            raise api_error(
                400,
                f"Model does not support chat completions: {model_id}",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        if model.id not in seen:
            candidates.append(model)
            seen.add(model.id)

    candidates = _filter_candidates_soft_data_collection(candidates, prefs, _apply_provider_filters)
    if not candidates:
        raise api_error(
            400,
            "No route candidates match the requested provider filters",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    candidates = _sort_candidates(candidates, prefs)
    if not prefs.allow_fallbacks:
        return candidates[:1]
    return candidates


def chat_route_endpoint_candidates(
    body: dict[str, Any], settings: Settings
) -> list[tuple[Model, ModelEndpoint]]:
    raw_ids, prefs = _routing_for_body(body, settings)
    candidates: list[tuple[Model, ModelEndpoint]] = []
    seen: set[str] = set()
    for model_id in raw_ids:
        model = MODELS.get(model_id)
        if model is None or not model.supports_chat:
            raise api_error(
                400,
                f"Model does not support chat completions: {model_id}",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        for endpoint in endpoints_for_model(model.id):
            if endpoint.id in seen:
                continue
            candidates.append((model, endpoint))
            seen.add(endpoint.id)

    candidates = _filter_candidates_soft_data_collection(
        candidates, prefs, _apply_endpoint_provider_filters
    )
    if not candidates:
        raise api_error(
            400,
            "No route candidates match the requested provider filters",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    candidates = _sort_endpoint_candidates(candidates, prefs)
    if not prefs.allow_fallbacks:
        return candidates[:1]
    return candidates


def catalog_endpoint_candidates(
    model: Model,
    prefs: RoutePreferences,
) -> list[tuple[Model, ModelEndpoint]]:
    """Endpoint candidates for the public model-endpoints catalog route.

    Unlike inference routing, this intentionally does not require
    `supports_chat`; the OpenRouter-compatible endpoint catalog should be able
    to describe any served model while still honoring provider filters.
    """
    candidates = [(model, endpoint) for endpoint in endpoints_for_model(model.id)]
    candidates = _filter_candidates_soft_data_collection(
        candidates, prefs, _apply_endpoint_provider_filters
    )
    candidates = _sort_endpoint_candidates(candidates, prefs)
    if not prefs.allow_fallbacks:
        return candidates[:1]
    return candidates


def embeddings_route_endpoint_candidates(
    body: dict[str, Any], settings: Settings
) -> list[tuple[Model, ModelEndpoint]]:
    """Endpoint candidates for an embeddings request — the gateway-authorize
    analogue of `chat_route_endpoint_candidates`, accepting only
    `supports_embeddings` models. Cost falls out of the per-endpoint prompt
    price (completion price is 0 on embedding endpoints), so the enclave can
    authorize + bill an embeddings route exactly like a chat one."""
    raw_ids, prefs = _routing_for_body(body, settings)
    candidates: list[tuple[Model, ModelEndpoint]] = []
    seen: set[str] = set()
    for model_id in raw_ids:
        model = MODELS.get(model_id)
        if model is None or not model.supports_embeddings:
            raise api_error(
                400,
                f"Model does not support embeddings: {model_id}",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        for endpoint in endpoints_for_model(model.id):
            if endpoint.id in seen:
                continue
            candidates.append((model, endpoint))
            seen.add(endpoint.id)

    candidates = _filter_candidates_soft_data_collection(
        candidates, prefs, _apply_endpoint_provider_filters
    )
    if not candidates:
        raise api_error(
            400,
            "No route candidates match the requested provider filters",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    candidates = _sort_endpoint_candidates(candidates, prefs)
    if not prefs.allow_fallbacks:
        return candidates[:1]
    return candidates


def provider_route_preferences(body: dict[str, Any]) -> RoutePreferences:
    raw = body.get("provider")
    if not isinstance(raw, dict):
        return RoutePreferences()

    order = tuple(_provider_filter_list("order", raw.get("order")))
    only = frozenset(_provider_filter_list("only", raw.get("only")))
    ignore = frozenset(_provider_filter_list("ignore", raw.get("ignore")))
    allow_fallbacks = raw.get("allow_fallbacks")
    if allow_fallbacks is None:
        allow_fallbacks_bool = True
    elif isinstance(allow_fallbacks, bool):
        allow_fallbacks_bool = allow_fallbacks
    else:
        raise api_error(400, "provider.allow_fallbacks must be a boolean", ErrorType.BAD_REQUEST)

    data_collection = raw.get("data_collection")
    if data_collection is not None:
        data_collection = str(data_collection).strip().lower()
        if data_collection not in {"allow", "deny"}:
            raise api_error(
                400,
                "provider.data_collection must be 'allow' or 'deny'",
                ErrorType.BAD_REQUEST,
            )

    sort = _sort_mode(raw.get("sort"))
    usage_type = _usage_type(raw.get("usage") or raw.get("usage_type") or raw.get("billing"))
    provider_jurisdiction = _provider_jurisdiction(
        raw.get("jurisdiction")
        or raw.get("country")
        or raw.get("headquarters_country")
        or raw.get("provider_country")
    )

    # provider.min_privacy: route only to providers whose posture clears
    # this bar. Accepts friendly names ("zdr", "confidential", "no_store",
    # "any"). Unknown values are a 400 so a typo can't silently downgrade
    # the privacy a caller asked for.
    min_privacy_rank = 0
    min_privacy = raw.get("min_privacy")
    if min_privacy is not None:
        key = str(min_privacy).strip().lower()
        if key not in PRIVACY_TIER_ALIASES:
            raise api_error(
                400,
                "provider.min_privacy must be one of: any, no_store, zdr, confidential (alias: e2ee)",
                ErrorType.BAD_REQUEST,
            )
        min_privacy_rank = PRIVACY_TIER_ALIASES[key]

    return RoutePreferences(
        order=order,
        only=only,
        ignore=ignore,
        allow_fallbacks=allow_fallbacks_bool,
        data_collection=data_collection,
        min_privacy_rank=min_privacy_rank,
        sort=sort,
        usage_type=usage_type,
        provider_jurisdiction=provider_jurisdiction,
    )


def _strip_variant_suffix(model_id: str) -> tuple[str, dict[str, str]]:
    """Detect an OpenRouter-style variant suffix on the model id. Returns
    `(stripped_id, overrides)`. `overrides` is empty if no suffix matches.
    Multiple suffixes don't compose today — first match wins."""
    for suffix, (key, value) in _VARIANT_SUFFIXES.items():
        if model_id.endswith(suffix):
            return model_id[: -len(suffix)], {key: value}
    return model_id, {}


def _routing_for_body(
    body: dict[str, Any], settings: Settings
) -> tuple[list[str], RoutePreferences]:
    """Strip variant suffixes from `body.model` / `body.models[]`, expand
    AUTO, build the RoutePreferences. Suffix-derived overrides win over
    body-set fields (per OpenRouter: the suffix is the explicit shorthand
    and is meant to be authoritative)."""
    ids, overrides = _requested_model_ids(body, settings)
    prefs = provider_route_preferences(body)
    if "sort" in overrides:
        prefs = dataclasses.replace(prefs, sort=overrides["sort"])
    if "order" in overrides:
        prefs = dataclasses.replace(
            prefs,
            order=tuple(_provider_filter_list("order", overrides["order"])),
        )
    if "only" in overrides:
        override_only = frozenset(_provider_filter_list("only", overrides["only"]))
        effective_only = override_only if not prefs.only else prefs.only & override_only
        prefs = dataclasses.replace(prefs, only=effective_only)
    if "min_privacy" in overrides:
        prefs = dataclasses.replace(
            prefs,
            min_privacy_rank=max(
                prefs.min_privacy_rank,
                PRIVACY_TIER_ALIASES[overrides["min_privacy"]],
            ),
        )
    if overrides.get("usage") == "Credits":
        if prefs.usage_type == "BYOK":
            raise api_error(
                400,
                "TrustedRouter orchestration models do not support BYOK routes",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        prefs = dataclasses.replace(prefs, usage_type="Credits")
    if "provider_jurisdiction" in overrides:
        jurisdiction = overrides["provider_jurisdiction"]
        if prefs.provider_jurisdiction and prefs.provider_jurisdiction != jurisdiction:
            raise api_error(
                400,
                "Requested model requires provider.jurisdiction='us'",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        prefs = dataclasses.replace(prefs, provider_jurisdiction=jurisdiction)
    return ids, prefs


# OpenAI-style dated snapshot suffix, e.g. the "-2025-04-14" in
# "gpt-4.1-2025-04-14". Anthropic-style undashed dates ("20241022") don't match.
_DATED_SNAPSHOT_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def resolve_model_alias(model_id: str) -> str:
    """Map a bare or dated OpenAI-style id to its canonical catalog id.

    The OpenAI SDK and LiteLLM send the *bare* name (`gpt-4.1`) or OpenAI's
    *dated snapshot* (`gpt-4.1-2025-04-14`); our catalog ids are vendor-prefixed
    (`openai/gpt-4.1`). Accept what that tooling sends so it works against TR with
    no client-side shim. Conservative by design: only rewrites when the resolved
    id actually exists in the catalog — a genuinely unknown model is returned
    unchanged and still surfaces MODEL_NOT_SUPPORTED downstream. Catalog ids
    (already vendor-prefixed) short-circuit on the first check, so real ids and
    meta ids (AUTO / fusion / zdr / …) are never altered.
    """
    if model_id in ROUTING_MODEL_ALIAS_TARGETS:
        return ROUTING_MODEL_ALIAS_TARGETS[model_id]
    if model_id in MODELS:
        return model_id
    base = _DATED_SNAPSHOT_RE.sub("", model_id)
    for candidate in (model_id, base):
        if candidate in MODELS:
            return candidate
        prefixed = f"openai/{candidate}"
        if prefixed in MODELS:
            return prefixed
    return model_id


def _requested_model_ids(
    body: dict[str, Any], settings: Settings
) -> tuple[list[str], dict[str, str]]:
    ids: list[str] = []
    overrides: dict[str, str] = {}

    def take(raw: str) -> None:
        stripped, ovr = _strip_variant_suffix(raw)
        stripped = resolve_model_alias(stripped)
        if stripped in META_MODEL_IDS:
            overrides["usage"] = "Credits"
        if stripped in US_PROVIDER_ONLY_MODEL_IDS:
            overrides["provider_jurisdiction"] = PROVIDER_JURISDICTION_US
        if stripped in {
            SYNTH_MODEL_ID,
            SYNTH_CODE_MODEL_ID,
            FUSION_MODEL_ID,
            FUSION_CODE_MODEL_ID,
            SELECTOR_MODEL_ID,
            MAPREDUCE_MODEL_ID,
        }:
            raise api_error(
                501,
                "TrustedRouter orchestration models execute only inside the attested gateway; control-plane routing must not silently degrade to a single model",
                ErrorType.ENDPOINT_NOT_SUPPORTED,
            )
        if ovr:
            overrides.update(ovr)
        enforced_privacy_tier = ROUTING_MODEL_MIN_PRIVACY_TIERS.get(stripped)
        if enforced_privacy_tier is not None:
            overrides["min_privacy"] = (
                "e2ee" if enforced_privacy_tier >= 3 else "zdr"
            )
        if stripped == ZDR_MODEL_ID:
            overrides["order"] = (
                "anthropic,openai,google-vertex,google-ai-studio,tinfoil,venice,phala"
            )
        elif stripped == E2E_MODEL_ID:
            overrides["order"] = "tinfoil,venice,phala"
        elif stripped == EU_MODEL_ID:
            provider_order = ",".join(EU_FOCUSED_PROVIDER_ORDER)
            overrides["order"] = provider_order
            overrides["only"] = provider_order
        ids.extend(_expand_model_id(stripped, settings))

    model_id = str(body.get("model") or "").strip()
    if model_id:
        take(model_id)

    fallback_models = body.get("models")
    if fallback_models is not None:
        if not isinstance(fallback_models, list):
            raise api_error(400, "models must be an array of model IDs", ErrorType.BAD_REQUEST)
        for item in fallback_models:
            if not isinstance(item, str) or not item.strip():
                raise api_error(400, "models must contain only model IDs", ErrorType.BAD_REQUEST)
            take(item.strip())

    if not ids:
        raise api_error(400, "model is required", ErrorType.BAD_REQUEST)
    return ids, overrides


def _expand_model_id(model_id: str, settings: Settings) -> list[str]:
    if model_id == AUTO_MODEL_ID:
        return [candidate.id for candidate in auto_candidate_models(settings.auto_model_order)]
    meta_candidates = meta_candidate_models(model_id)
    if meta_candidates:
        return [candidate.id for candidate in meta_candidates]
    return [model_id]


def _filter_candidates_soft_data_collection(
    candidates: list[_CandidateT],
    prefs: RoutePreferences,
    apply_fn: Callable[[list[_CandidateT], RoutePreferences], list[_CandidateT]],
) -> list[_CandidateT]:
    """Apply provider filters with data_collection='deny' as a soft preference.

    Some OpenRouter-migrated clients send this compatibility flag on every request
    unconditionally, so it must not hard-fail routing when it is the only filter
    emptying an otherwise valid route. Explicit privacy floors and provider
    inclusion/exclusion filters remain hard.
    """
    filtered = apply_fn(candidates, prefs)
    if not filtered and prefs.data_collection == "deny":
        filtered = apply_fn(candidates, dataclasses.replace(prefs, data_collection=None))
    return filtered


def _apply_provider_filters(candidates: list[Model], prefs: RoutePreferences) -> list[Model]:
    out: list[Model] = []
    for model in candidates:
        provider = PROVIDERS[model.provider]
        if prefs.only and model.provider not in prefs.only:
            continue
        if model.provider in prefs.ignore:
            continue
        if not _provider_matches_jurisdiction(provider, prefs.provider_jurisdiction):
            continue
        # "deny" = no data collection — require at least the no-store
        # tier. Keyed off the privacy tier (not raw stores_content) so
        # ZDR/confidential providers, which carry the conservative
        # stores_content=True default, are correctly kept.
        if (
            prefs.data_collection == "deny"
            and model_max_privacy_tier(model) < PRIVACY_TIER_NO_STORE
        ):
            continue
        # Keep a model if ANY provider that serves it can meet the
        # requested privacy bar — a model like deepseek-v3.2 is no-store
        # via deepseek but confidential via phala, so it stays in a
        # confidential request. The endpoint-level filter then narrows to
        # the qualifying provider when the gateway picks an endpoint.
        if prefs.min_privacy_rank and model_max_privacy_tier(model) < prefs.min_privacy_rank:
            continue
        out.append(model)
    return out


def _sort_candidates(candidates: list[Model], prefs: RoutePreferences) -> list[Model]:
    with_index = list(enumerate(candidates))
    provider_order = {provider: index for index, provider in enumerate(prefs.order)}

    def key(item: tuple[int, Model]) -> tuple[int, int, int]:
        original_index, model = item
        order_rank = provider_order.get(model.provider, len(provider_order))
        if prefs.sort == "price":
            sort_rank = (
                model.prompt_price_microdollars_per_million_tokens
                + model.completion_price_microdollars_per_million_tokens
            )
        elif prefs.sort in {"latency", "throughput"}:
            sort_rank = _THROUGHPUT_RANK.get(model.provider, 50)
        else:
            # Default: preserve the caller's explicit `models` array order. The
            # reliability preference is applied at the ENDPOINT level (which host
            # serves a model), NOT here — reordering an explicit fallback list
            # would violate the caller's intended order.
            sort_rank = original_index
        return order_rank, sort_rank, original_index

    return [model for _, model in sorted(with_index, key=key)]


def _apply_endpoint_provider_filters(
    candidates: list[tuple[Model, ModelEndpoint]],
    prefs: RoutePreferences,
) -> list[tuple[Model, ModelEndpoint]]:
    out: list[tuple[Model, ModelEndpoint]] = []
    for model, endpoint in candidates:
        provider = PROVIDERS[endpoint.provider]
        if prefs.only and endpoint.provider not in prefs.only:
            continue
        if endpoint.provider in prefs.ignore:
            continue
        if not _provider_matches_jurisdiction(provider, prefs.provider_jurisdiction):
            continue
        # "deny" = no data collection — require at least the no-store
        # tier. Keyed off the privacy tier (not raw stores_content) so
        # ZDR/confidential providers, which carry the conservative
        # stores_content=True default, are correctly kept.
        if (
            prefs.data_collection == "deny"
            and endpoint_privacy_tier(endpoint) < PRIVACY_TIER_NO_STORE
        ):
            continue
        if prefs.min_privacy_rank and endpoint_privacy_tier(endpoint) < prefs.min_privacy_rank:
            continue
        if prefs.usage_type is not None and endpoint.usage_type != prefs.usage_type:
            continue
        out.append((model, endpoint))
    return out


def _sort_endpoint_candidates(
    candidates: list[tuple[Model, ModelEndpoint]],
    prefs: RoutePreferences,
) -> list[tuple[Model, ModelEndpoint]]:
    with_index = list(enumerate(candidates))
    provider_order = {provider: index for index, provider in enumerate(prefs.order)}
    candidate_model_ids = {model.id for model, _endpoint in candidates}
    single_model_id = next(iter(candidate_model_ids)) if len(candidate_model_ids) == 1 else None

    def key(item: tuple[int, tuple[Model, ModelEndpoint]]) -> tuple[int, int, int]:
        original_index, (model, endpoint) = item
        order_rank = provider_order.get(endpoint.provider, len(provider_order))
        if prefs.sort == "price":
            sort_rank = (
                endpoint.prompt_price_microdollars_per_million_tokens
                + endpoint.completion_price_microdollars_per_million_tokens
            )
        elif prefs.sort in {"latency", "throughput"}:
            sort_rank = _THROUGHPUT_RANK.get(endpoint.provider, 50)
        else:
            # Default: reliability-informed preference (Phase 4), catalog order
            # preserved within a tier via the original_index tiebreaker below.
            # A provider preference for one model must not promote that model
            # ahead of a caller's primary model or a meta-router's model order.
            model_preference = (
                _MODEL_PROVIDER_PREFERENCE.get(model.id, {})
                if model.id == single_model_id
                else {}
            )
            sort_rank = model_preference.get(
                endpoint.provider,
                _PROVIDER_PREFERENCE.get(endpoint.provider, _DEFAULT_PROVIDER_PREFERENCE),
            )
        return order_rank, sort_rank, original_index

    return [candidate for _, candidate in sorted(with_index, key=key)]


def _provider_slug(value: str) -> str:
    slug = value.strip().lower().replace("_", "-").replace(" ", "-")
    return _PROVIDER_ALIASES.get(slug, slug)


def _provider_filter_list(field: str, value: Any) -> list[str]:
    out: list[str] = []
    for item in _string_list(field, value):
        raw_slug = item.strip().lower().replace("_", "-").replace(" ", "-")
        slugs = _PROVIDER_GROUP_ALIASES.get(raw_slug, (_provider_slug(item),))
        if any(slug in _ROUTER_PROVIDER_SLUGS for slug in slugs):
            raise api_error(
                400,
                (
                    f"Routing filters cannot contain router name '{item}'. "
                    "Use model='trustedrouter/zdr' or another TrustedRouter alias, "
                    "and omit the router from provider filters."
                ),
                ErrorType.BAD_REQUEST,
            )
        for slug in slugs:
            if slug not in PROVIDERS:
                raise api_error(
                    400,
                    f"Unknown provider in provider.{field}: {item}",
                    ErrorType.BAD_REQUEST,
                )
            if slug not in out:
                out.append(slug)
    return out


def _provider_jurisdiction(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise api_error(
            400,
            "provider.jurisdiction must be 'us'",
            ErrorType.BAD_REQUEST,
        )
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    if normalized in {"us", "usa", "united_states", "united_states_of_america"}:
        return PROVIDER_JURISDICTION_US
    raise api_error(
        400,
        "provider.jurisdiction currently supports only 'us'",
        ErrorType.BAD_REQUEST,
    )


def _provider_matches_jurisdiction(provider: Any, jurisdiction: str | None) -> bool:
    if jurisdiction is None:
        return True
    if jurisdiction == PROVIDER_JURISDICTION_US:
        return provider.provider_headquarters_country == PROVIDER_JURISDICTION_US
    return False


def _string_list(field: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        values = value
    else:
        raise api_error(
            400,
            f"provider.{field} must be an array of strings or a comma-separated string",
            ErrorType.BAD_REQUEST,
        )
    out: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise api_error(
                400,
                f"provider.{field} must contain only provider names",
                ErrorType.BAD_REQUEST,
            )
        out.append(item)
    return out


def _sort_mode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip().lower()
    elif isinstance(value, dict):
        raw = value.get("sort") or value.get("strategy") or value.get("by")
        candidate = str(raw or "").strip().lower()
    else:
        raise api_error(400, "provider.sort must be a string or object", ErrorType.BAD_REQUEST)
    if candidate in {"price", "latency", "throughput"}:
        return candidate
    if candidate:
        raise api_error(400, "provider.sort is unsupported", ErrorType.BAD_REQUEST)
    return None


def _usage_type(value: Any) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if candidate in {"byok", "bring-your-own-key"}:
        return "BYOK"
    if candidate in {"credits", "credit", "prepaid"}:
        return "Credits"
    raise api_error(400, "provider.usage must be 'credits' or 'byok'", ErrorType.BAD_REQUEST)
