"""Reusable catalog and measurement evidence for public SEO landing pages.

The catalog portion is computed in memory. Performance rows come from the same
five minute snapshot cache as the leaderboard, so rendering an SEO page never
creates a live analytics query of its own.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from trusted_router.benchmark_scores import scores_for_model
from trusted_router.catalog import (
    META_MODEL_IDS,
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoint_zero_data_retention,
    endpoints_for_model,
)
from trusted_router.measured import measured_snapshot
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.provider_branding import provider_logo_url

_MAX_MODELS = 6
_MAX_PROVIDERS = 8

_DEFAULT_MODEL_IDS: tuple[str, ...] = (
    "anthropic/claude-opus-4.8",
    "openai/gpt-5.5",
    "google/gemini-3.5-flash",
    "moonshotai/kimi-k2.7-code",
    "z-ai/glm-5.2",
    "minimax/minimax-m3",
)

_PAGE_FOCUS_TERMS: dict[str, tuple[str, ...]] = {
    "azure-openai-alternative": ("gpt-5", "claude-opus", "gemini"),
    "aws-bedrock-alternative": ("claude", "llama", "mistral"),
    "china-ai-models": ("deepseek", "kimi", "glm", "qwen", "minimax"),
    "chinese-ai-models-us-hosted": ("deepseek", "kimi", "glm", "qwen", "minimax"),
    "claude-api-privacy": ("claude-opus", "claude-sonnet", "claude-haiku"),
    "deepseek-api-privacy": ("deepseek-v4", "deepseek-v3", "deepseek-r1"),
    "eu-ai-models": ("mistral",),
    "gemini-flash-alternative": ("gemini-3.5-flash", "gemini-3.1-flash", "gemma"),
    "glm-5-api": ("glm-5.2", "glm-5.1", "glm-5"),
    "gpt-oss-120b-api": ("gpt-oss-120b",),
    "groq-alternative": ("gpt-oss-120b", "llama", "qwen"),
    "kimi-k2-api": ("kimi-k2.7", "kimi-k2.6", "kimi-k2.5"),
    "llm-document-processing": ("gemini", "deepseek-ocr", "qwen-vl"),
    "minimax-m3-api": ("minimax-m3", "minimax-m2"),
    "tinfoil-alternative": ("glm", "qwen", "deepseek"),
    "us-ai-models": ("gpt-5", "claude-opus", "gemini", "nemotron", "llama"),
    "vertex-ai-alternative": ("gemini", "claude"),
}

_DEFAULT_PROVIDER_SLUGS: tuple[str, ...] = (
    "tinfoil",
    "anthropic",
    "openai",
    "google-vertex",
    "cerebras",
    "together",
    "nebius",
    "minimax",
)


def seo_catalog_evidence(page_key: str, *, test_mode: bool = False) -> dict[str, object]:
    public_models = [model for model in MODELS.values() if model.id not in META_MODEL_IDS]
    public_model_ids = {model.id for model in public_models}
    public_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.model_id in public_model_ids
    ]
    snapshot = measured_snapshot(test_mode=test_mode)
    measured_models = _mapping_rows(snapshot.get("models"))
    measured_providers = _mapping_rows(snapshot.get("providers"))
    measured_by_model: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in measured_models:
        model_id = row.get("model")
        if isinstance(model_id, str):
            measured_by_model[model_id].append(row)

    selected_models = _select_models(
        page_key,
        public_models,
        measured_by_model=measured_by_model,
    )
    model_rows = [
        _model_row(model, measured_rows=measured_by_model.get(model.id, ()))
        for model in selected_models
    ]
    provider_rows = _provider_rows(
        selected_models,
        public_models=public_models,
        measured_rows=measured_providers,
    )
    return {
        "model_count": len(public_models),
        "provider_count": len(PROVIDERS),
        "route_count": len(public_endpoints),
        "zdr_route_count": sum(
            endpoint_zero_data_retention(endpoint) is True for endpoint in public_endpoints
        ),
        "e2e_route_count": sum(endpoint_e2ee(endpoint) is True for endpoint in public_endpoints),
        "measured_sample_count": _int_value(snapshot.get("total_samples")),
        "measured_provider_count": _int_value(snapshot.get("provider_count")),
        "generated_at": str(snapshot.get("generated_at") or ""),
        "models": model_rows,
        "providers": provider_rows,
    }


def _mapping_rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _select_models(
    page_key: str,
    models: Sequence[Model],
    *,
    measured_by_model: Mapping[str, Sequence[Mapping[str, object]]],
) -> list[Model]:
    by_id = {model.id: model for model in models}
    focus_terms = _PAGE_FOCUS_TERMS.get(page_key, ())
    focused = [
        model
        for model in models
        if any(term in f"{model.id} {model.name}".lower() for term in focus_terms)
    ]
    focused.sort(
        key=lambda model: (
            -_model_sample_count(measured_by_model.get(model.id, ())),
            -len(endpoints_for_model(model.id)),
            model.id,
        )
    )
    selected: list[Model] = []
    for model in focused:
        if model not in selected:
            selected.append(model)
        if len(selected) >= _MAX_MODELS:
            return selected
    for model_id in _DEFAULT_MODEL_IDS:
        default_model = by_id.get(model_id)
        if default_model is not None and default_model not in selected:
            selected.append(default_model)
        if len(selected) >= _MAX_MODELS:
            return selected
    measured = sorted(
        models,
        key=lambda model: (
            -_model_sample_count(measured_by_model.get(model.id, ())),
            -len(endpoints_for_model(model.id)),
            model.id,
        ),
    )
    for model in measured:
        if model not in selected:
            selected.append(model)
        if len(selected) >= _MAX_MODELS:
            break
    return selected


def _model_row(
    model: Model,
    *,
    measured_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    endpoints = endpoints_for_model(model.id)
    providers: list[dict[str, str]] = []
    seen: set[str] = set()
    for endpoint in endpoints:
        if endpoint.provider in seen:
            continue
        seen.add(endpoint.provider)
        provider = PROVIDERS.get(endpoint.provider)
        providers.append(
            {
                "slug": endpoint.provider,
                "name": provider.name if provider else endpoint.provider,
                "logo_url": provider_logo_url(endpoint.provider),
                "detail_href": f"/providers/{endpoint.provider}",
            }
        )
    representative = _representative_measurement(measured_rows)
    availability = None
    if representative is not None:
        availability = representative.get("provider_availability")
        if availability is None:
            availability = representative.get("uptime")
    return {
        "id": model.id,
        "name": model.name,
        "detail_href": f"/models/{model.id}",
        "context_length": f"{model.context_length:,}",
        "route_count": len(endpoints),
        "provider_count": len(providers),
        "providers": providers,
        "prompt_price": _endpoint_price_range(
            endpoints,
            "prompt_price_microdollars_per_million_tokens",
        ),
        "completion_price": _endpoint_price_range(
            endpoints,
            "completion_price_microdollars_per_million_tokens",
        ),
        "has_zdr": any(endpoint_zero_data_retention(endpoint) is True for endpoint in endpoints),
        "has_confidential": any(
            endpoint_confidential_compute(endpoint) is True for endpoint in endpoints
        ),
        "has_e2e": any(endpoint_e2ee(endpoint) is True for endpoint in endpoints),
        "benchmark_count": len(scores_for_model(model.id)),
        "measurement_provider": (
            str(representative.get("provider") or "") if representative else ""
        ),
        "p50_ttft_ms": representative.get("p50_ttft_ms") if representative else None,
        "p50_tokens_per_second": (
            representative.get("p50_tokens_per_second") if representative else None
        ),
        "availability": availability,
        "sample_count": _model_sample_count(measured_rows),
    }


def _provider_rows(
    selected_models: Sequence[Model],
    *,
    public_models: Sequence[Model],
    measured_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    measured_by_slug = {
        str(row.get("provider")): row
        for row in measured_rows
        if isinstance(row.get("provider"), str)
    }
    model_counts: dict[str, int] = defaultdict(int)
    for model in public_models:
        for slug in {endpoint.provider for endpoint in endpoints_for_model(model.id)}:
            model_counts[slug] += 1

    ordered_slugs: list[str] = []
    for model in selected_models:
        for endpoint in endpoints_for_model(model.id):
            if endpoint.provider not in ordered_slugs:
                ordered_slugs.append(endpoint.provider)
    for slug in _DEFAULT_PROVIDER_SLUGS:
        if slug in PROVIDERS and slug not in ordered_slugs:
            ordered_slugs.append(slug)
    return [
        _provider_row(
            slug,
            model_count=model_counts.get(slug, 0),
            measured=measured_by_slug.get(slug),
        )
        for slug in ordered_slugs[:_MAX_PROVIDERS]
    ]


def _provider_row(
    slug: str,
    *,
    model_count: int,
    measured: Mapping[str, object] | None,
) -> dict[str, object]:
    provider = PROVIDERS[slug]
    if provider.provider_e2ee and provider.provider_confidential_compute:
        privacy = "Provider E2EE"
    elif provider.provider_zero_data_retention:
        privacy = "ZDR"
    elif provider.prepaid_zero_data_retention:
        privacy = "ZDR on prepaid"
    elif provider.provider_confidential_compute:
        privacy = "Confidential compute"
    else:
        privacy = "Policy varies"
    availability = measured.get("provider_availability") if measured else None
    if measured and availability is None:
        availability = measured.get("uptime")
    return {
        "slug": slug,
        "name": provider.name,
        "detail_href": f"/providers/{slug}",
        "logo_url": provider_logo_url(slug),
        "privacy": privacy,
        "model_count": model_count,
        "p50_ttft_ms": measured.get("p50_ttft_ms") if measured else None,
        "p50_tokens_per_second": (measured.get("p50_tokens_per_second") if measured else None),
        "availability": availability,
        "sample_count": _int_value(measured.get("sample_count")) if measured else 0,
    }


def _representative_measurement(
    rows: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    if not rows:
        return None
    return min(
        rows,
        key=lambda row: (
            -_int_value(row.get("sample_count")),
            row.get("p50_ttft_ms") is None,
            _int_value(row.get("p50_ttft_ms")),
            str(row.get("provider") or ""),
        ),
    )


def _model_sample_count(rows: Sequence[Mapping[str, object]]) -> int:
    return sum(_int_value(row.get("sample_count")) for row in rows)


def _endpoint_price_range(endpoints: Sequence[ModelEndpoint], attr: str) -> str:
    values = [int(getattr(endpoint, attr)) for endpoint in endpoints if getattr(endpoint, attr) > 0]
    if not values:
        return "Selected route"
    low = _format_price(min(values))
    high = _format_price(max(values))
    return low if low == high else f"{low} to {high}"


def _format_price(microdollars_per_million: int) -> str:
    value = Decimal(microdollars_per_million) / Decimal(MICRODOLLARS_PER_DOLLAR)
    return f"${value.normalize():f}/1M"


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
