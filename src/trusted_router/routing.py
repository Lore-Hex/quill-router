from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.catalog import (
    AUTO_MODEL_ID,
    MODELS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    auto_candidate_models,
    endpoints_for_model,
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


_PROVIDER_ALIASES = {
    "google-vertex": "vertex",
    "vertex-ai": "vertex",
    "vertex": "vertex",
    "google": "gemini",
    "google-ai-studio": "gemini",
    "mistralai": "mistral",
    "mistral-ai": "mistral",
    "moonshot": "kimi",
    "moonshot-ai": "kimi",
    "kimi": "kimi",
}

_THROUGHPUT_RANK = {
    "cerebras": 0,
    "vertex": 1,
    "gemini": 2,
    "deepseek": 3,
    "kimi": 4,
    "mistral": 5,
    "openai": 6,
    "anthropic": 7,
    "trustedrouter": 99,
}


def chat_route_candidates(body: dict[str, Any], settings: Settings) -> list[Model]:
    raw_ids = _requested_model_ids(body, settings)
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

    prefs = provider_route_preferences(body)
    candidates = _apply_provider_filters(candidates, prefs)
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


def chat_route_endpoint_candidates(body: dict[str, Any], settings: Settings) -> list[tuple[Model, ModelEndpoint]]:
    raw_ids = _requested_model_ids(body, settings)
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

    prefs = provider_route_preferences(body)
    candidates = _apply_endpoint_provider_filters(candidates, prefs)
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

    order = tuple(_provider_slug(item) for item in _string_list(raw.get("order")))
    only = frozenset(_provider_slug(item) for item in _string_list(raw.get("only")))
    ignore = frozenset(_provider_slug(item) for item in _string_list(raw.get("ignore")))
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

    return RoutePreferences(
        order=order,
        only=only,
        ignore=ignore,
        allow_fallbacks=allow_fallbacks_bool,
        data_collection=data_collection,
        sort=sort,
        usage_type=usage_type,
    )


def _requested_model_ids(body: dict[str, Any], settings: Settings) -> list[str]:
    ids: list[str] = []
    model_id = str(body.get("model") or "").strip()
    if model_id:
        ids.extend(_expand_model_id(model_id, settings))

    fallback_models = body.get("models")
    if fallback_models is not None:
        if not isinstance(fallback_models, list):
            raise api_error(400, "models must be an array of model IDs", ErrorType.BAD_REQUEST)
        for item in fallback_models:
            if not isinstance(item, str) or not item.strip():
                raise api_error(400, "models must contain only model IDs", ErrorType.BAD_REQUEST)
            ids.extend(_expand_model_id(item.strip(), settings))

    if not ids:
        raise api_error(400, "model is required", ErrorType.BAD_REQUEST)
    return ids


def _expand_model_id(model_id: str, settings: Settings) -> list[str]:
    if model_id == AUTO_MODEL_ID:
        return [candidate.id for candidate in auto_candidate_models(settings.auto_model_order)]
    return [model_id]


def _apply_provider_filters(candidates: list[Model], prefs: RoutePreferences) -> list[Model]:
    out: list[Model] = []
    for model in candidates:
        provider = PROVIDERS[model.provider]
        if prefs.only and model.provider not in prefs.only:
            continue
        if model.provider in prefs.ignore:
            continue
        if prefs.data_collection == "deny" and provider.stores_content:
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
        if prefs.data_collection == "deny" and provider.stores_content:
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

    def key(item: tuple[int, tuple[Model, ModelEndpoint]]) -> tuple[int, int, int]:
        original_index, (_model, endpoint) = item
        order_rank = provider_order.get(endpoint.provider, len(provider_order))
        if prefs.sort == "price":
            sort_rank = (
                endpoint.prompt_price_microdollars_per_million_tokens
                + endpoint.completion_price_microdollars_per_million_tokens
            )
        elif prefs.sort in {"latency", "throughput"}:
            sort_rank = _THROUGHPUT_RANK.get(endpoint.provider, 50)
        else:
            sort_rank = original_index
        return order_rank, sort_rank, original_index

    return [candidate for _, candidate in sorted(with_index, key=key)]


def _provider_slug(value: str) -> str:
    slug = value.strip().lower().replace("_", "-").replace(" ", "-")
    return _PROVIDER_ALIASES.get(slug, slug)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise api_error(400, "provider routing lists must be arrays of strings", ErrorType.BAD_REQUEST)
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise api_error(400, "provider routing lists must contain strings", ErrorType.BAD_REQUEST)
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
