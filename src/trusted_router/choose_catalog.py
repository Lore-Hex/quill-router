"""Compact, endpoint-scoped data contract for the public model picker.

The full OpenRouter-compatible catalog intentionally contains rich endpoint
metadata and is too large for a marketing-page dependency. This module emits
only the facts the picker needs, while preserving the exact provider,
credential path, privacy tier, and integer microdollar price for every route.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trusted_router.ai_iq import ai_iq_catalog_payload
from trusted_router.catalog import (
    AUTO_MODEL_ID,
    CHEAP_MODEL_ID,
    E2E_MODEL_ID,
    META_MODEL_IDS,
    MODELS,
    PRIVACY_TIER_LABELS,
    PROVIDERS,
    ROUTING_MODEL_MIN_PRIVACY_TIERS,
    SYNTH_MODEL_ID,
    ZDR_MODEL_ID,
    Model,
    ModelEndpoint,
    endpoint_privacy_tier,
    endpoint_zero_data_retention,
    endpoints_for_model,
    model_open_weights,
    model_provider_policy_url,
    model_to_openrouter_shape,
)
from trusted_router.measured import measured_snapshot
from trusted_router.storage_models import utcnow

_ROUTE_IDS = (
    AUTO_MODEL_ID,
    CHEAP_MODEL_ID,
    ZDR_MODEL_ID,
    E2E_MODEL_ID,
    SYNTH_MODEL_ID,
)

_ROUTE_DESCRIPTIONS = {
    AUTO_MODEL_ID: (
        "Ranks configured model candidates and rolls over across healthy providers. "
        "No upstream privacy floor is implied."
    ),
    CHEAP_MODEL_ID: (
        "Chooses from the lowest-cost paid candidates. No upstream privacy floor is implied."
    ),
    ZDR_MODEL_ID: "Enforces a zero-retention or stronger provider endpoint for every attempt.",
    E2E_MODEL_ID: (
        "Enforces provider confidential compute plus provider-side end-to-end encryption."
    ),
    SYNTH_MODEL_ID: (
        "Runs a model panel, judge, and synthesizer. Every inner inference call is billable."
    ),
}

_DIMENSION_TAGS = {
    "abstract-reasoning": "reasoning",
    "mathematical-reasoning": "math",
    "scientific-reasoning": "science",
    "frontend-engineering": "coding",
    "backend-engineering": "coding",
    "computer-use": "agentic",
    "reliability": "reliable",
    "multimodal": "vision",
}


def choose_catalog_payload(*, test_mode: bool = False) -> dict[str, Any]:
    catalog_models = _public_chat_models()
    quality = ai_iq_catalog_payload(
        (model.id for model in catalog_models),
        test_mode=test_mode,
    )
    measured = measured_snapshot(test_mode=test_mode)
    return build_choose_catalog_payload(
        catalog_models=catalog_models,
        quality_models=quality.get("models", {}),
        quality_updated_at=quality.get("updated_at", ""),
        measured=measured,
    )


def build_choose_catalog_payload(
    *,
    catalog_models: list[Model],
    quality_models: Mapping[str, Mapping[str, Any]],
    quality_updated_at: str,
    measured: Mapping[str, Any],
) -> dict[str, Any]:
    measured_by_route = {
        (str(row.get("model", "")), str(row.get("provider", ""))): row
        for row in measured.get("models", [])
        if isinstance(row, Mapping)
    }
    models: list[dict[str, Any]] = []
    catalog_route_count = 0
    for model in catalog_models:
        endpoints = list(endpoints_for_model(model.id))
        catalog_route_count += len(endpoints)
        quality = quality_models.get(model.id)
        if quality is None or not endpoints or _positive_quality_score(quality) is None:
            continue
        endpoint_rows = [
            _endpoint_row(
                model.id,
                endpoint,
                measured_by_route.get((model.id, endpoint.provider)),
            )
            for endpoint in endpoints
        ]
        endpoint_rows.sort(
            key=lambda row: (
                -int(row["privacy_tier"]),
                int(row["prompt_price_microdollars_per_million_tokens"])
                + int(row["completion_price_microdollars_per_million_tokens"]),
                str(row["provider"]),
                str(row["usage_type"]),
            )
        )
        models.append(
            {
                "id": model.id,
                "name": model.name,
                "context_length": model.context_length,
                "open_weights": model_open_weights(model),
                "quality": _quality_row(quality),
                "tags": _model_tags(model.id, quality),
                "endpoints": endpoint_rows,
            }
        )

    models.sort(
        key=lambda row: (
            -int(row["quality"]["score"]),
            str(row["name"]).lower(),
            str(row["id"]),
        )
    )
    return {
        "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "quality_updated_at": quality_updated_at,
        "performance_updated_at": str(measured.get("generated_at", "")),
        "catalog_model_count": len(catalog_models),
        "catalog_route_count": catalog_route_count,
        "evaluated_model_count": len(models),
        "models": models,
        "routes": [_meta_route_row(route_id) for route_id in _ROUTE_IDS if route_id in MODELS],
        "privacy_tiers": [
            {"tier": tier, "label": label}
            for tier, label in sorted(PRIVACY_TIER_LABELS.items())
        ],
    }


def _public_chat_models() -> list[Model]:
    return sorted(
        (
            model
            for model in MODELS.values()
            if model.id not in META_MODEL_IDS
            and model.supports_chat
            and not model.hidden_public_metadata
            and endpoints_for_model(model.id)
        ),
        key=lambda model: model.id,
    )


def _endpoint_row(
    model_id: str,
    endpoint: ModelEndpoint,
    measured: Mapping[str, Any] | None,
) -> dict[str, Any]:
    provider = PROVIDERS[endpoint.provider]
    tier = endpoint_privacy_tier(endpoint)
    performance = None
    if measured is not None:
        performance = {
            "sample_count": int(measured.get("sample_count") or 0),
            "uptime": measured.get("uptime"),
            "p50_ttft_ms": measured.get("p50_ttft_ms"),
            "p50_tokens_per_second": measured.get("p50_tokens_per_second"),
            "last_seen": measured.get("last_seen"),
        }
    return {
        "provider": endpoint.provider,
        "provider_name": provider.name,
        "usage_type": endpoint.usage_type,
        "privacy_tier": tier,
        "privacy_tier_label": PRIVACY_TIER_LABELS[tier],
        "zero_data_retention": endpoint_zero_data_retention(endpoint),
        "provider_policy_url": model_provider_policy_url(model_id, endpoint.provider),
        "prompt_price_microdollars_per_million_tokens": (
            endpoint.prompt_price_microdollars_per_million_tokens
        ),
        "completion_price_microdollars_per_million_tokens": (
            endpoint.completion_price_microdollars_per_million_tokens
        ),
        "performance": performance,
    }


def _quality_row(quality: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = quality.get("dimensions")
    return {
        "score": int(quality.get("iq") or 0),
        "rank": int(quality["rank"]) if quality.get("rank") is not None else None,
        "url": str(quality.get("url") or ""),
        "source": "AI IQ",
        "dimensions": dict(dimensions) if isinstance(dimensions, Mapping) else {},
    }


def _positive_quality_score(quality: Mapping[str, Any]) -> int | None:
    value = quality.get("iq")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return None


def _model_tags(model_id: str, quality: Mapping[str, Any]) -> list[str]:
    dimensions = quality.get("dimensions")
    ranked_dimensions: list[tuple[str, int]] = []
    if isinstance(dimensions, Mapping):
        for name, value in dimensions.items():
            if isinstance(value, int | float):
                ranked_dimensions.append((str(name), int(value)))
    ranked_dimensions.sort(key=lambda item: (-item[1], item[0]))
    tags: list[str] = []
    for name, _score in ranked_dimensions:
        tag = _DIMENSION_TAGS.get(name, name.replace("-", " "))
        if tag not in tags:
            tags.append(tag)
    lowered = model_id.lower()
    for needle, tag in (
        ("coder", "coding"),
        ("code", "coding"),
        ("vision", "vision"),
        ("-vl", "vision"),
        ("reason", "reasoning"),
        ("prover", "math"),
    ):
        if needle in lowered and tag not in tags:
            tags.insert(0, tag)
    return tags[:5]


def _meta_route_row(model_id: str) -> dict[str, Any]:
    shape = model_to_openrouter_shape(MODELS[model_id])
    trustedrouter = shape["trustedrouter"]
    assert isinstance(trustedrouter, dict)
    floor = ROUTING_MODEL_MIN_PRIVACY_TIERS.get(model_id, 0)
    component_usage = model_id == SYNTH_MODEL_ID
    return {
        "id": model_id,
        "name": model_id.removeprefix("trustedrouter/"),
        "description": _ROUTE_DESCRIPTIONS[model_id],
        "min_privacy_tier": floor,
        "min_privacy_label": PRIVACY_TIER_LABELS[floor],
        "pricing_mode": "component_usage" if component_usage else "selected_route",
        "prompt_price_min_microdollars_per_million_tokens": (
            None
            if component_usage
            else trustedrouter["prompt_price_microdollars_per_million_tokens"]
        ),
        "prompt_price_max_microdollars_per_million_tokens": (
            None
            if component_usage
            else trustedrouter.get(
                "prompt_price_max_microdollars_per_million_tokens",
                trustedrouter["prompt_price_microdollars_per_million_tokens"],
            )
        ),
        "completion_price_min_microdollars_per_million_tokens": (
            None
            if component_usage
            else trustedrouter["completion_price_microdollars_per_million_tokens"]
        ),
        "completion_price_max_microdollars_per_million_tokens": (
            None
            if component_usage
            else trustedrouter.get(
                "completion_price_max_microdollars_per_million_tokens",
                trustedrouter["completion_price_microdollars_per_million_tokens"],
            )
        ),
        "candidate_count": len(trustedrouter.get("auto_candidates") or []),
    }
