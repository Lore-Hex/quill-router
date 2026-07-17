from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.catalog import (
    EU_FOCUSED_PROVIDER_ORDER,
    MODELS,
    PRIVACY_TIER_LABELS,
    PROVIDER_JURISDICTION_US,
    PROVIDERS,
    endpoint_privacy_tier,
    endpoint_zero_data_retention,
    endpoint_zero_data_retention_scope,
    model_provider_policy,
    model_provider_policy_url,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.money import microdollars_per_million_tokens_to_token_decimal
from trusted_router.regions import choose_region, region_payload
from trusted_router.routing import catalog_endpoint_candidates, provider_route_preferences


def _set_provider_query(raw: dict[str, Any], key: str, value: str) -> None:
    existing = raw.get(key)
    if existing is None:
        raw[key] = value
    elif isinstance(existing, list):
        existing.append(value)
    else:
        raw[key] = [existing, value]


def _provider_query_body(request: Request) -> dict[str, Any]:
    provider: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key.startswith("provider[") and key.endswith("]"):
            field = key[len("provider[") : -1]
            if field.endswith("[]"):
                field = field[:-2]
            if field:
                _set_provider_query(provider, field, value)
        elif key.startswith("provider."):
            field = key.split(".", 1)[1]
            if field:
                _set_provider_query(provider, field, value)
    return {"provider": provider} if provider else {}


def _truthy_query(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _public_model_matches_filters(shape: dict[str, Any], request: Request) -> bool:
    trustedrouter = shape.get("trustedrouter")
    if not isinstance(trustedrouter, dict):
        return False
    if _truthy_query(request.query_params.get("open_weights")) and not trustedrouter.get(
        "open_weights"
    ):
        return False
    jurisdiction = (
        (
            request.query_params.get("provider[jurisdiction]")
            or request.query_params.get("provider.jurisdiction")
            or ""
        )
        .strip()
        .lower()
    )
    if jurisdiction in {"us", "usa", "united_states", "united-states"} and not trustedrouter.get(
        "us_provider_available"
    ):
        return False
    region = (
        (
            request.query_params.get("provider[region]")
            or request.query_params.get("provider.region")
            or request.query_params.get("region")
            or ""
        )
        .strip()
        .lower()
    )
    if region in {"eu", "europe"} and not trustedrouter.get("eu_focused_provider_available"):
        return False
    return True


def register_catalog_routes(router: APIRouter) -> None:
    @router.get("/embeddings/models")
    async def embeddings_models() -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [model_to_openrouter_shape(m) for m in MODELS.values() if m.supports_embeddings]
        }

    def _public_model_shapes(request: Request | None = None) -> list[dict[str, Any]]:
        # `internal_only` models (e.g. trustedrouter/monitor) must
        # never appear in the public catalog — they're system-internal
        # routing pools, not user-selectable. The shape itself carries
        # the flag; filter it BEFORE handing to callers so SDKs +
        # chat playground don't accidentally surface them.
        shapes = []
        for model in MODELS.values():
            shape = model_to_openrouter_shape(model)
            trustedrouter = shape.get("trustedrouter")
            if isinstance(trustedrouter, dict) and trustedrouter.get("internal_only"):
                continue
            if request is not None and not _public_model_matches_filters(shape, request):
                continue
            shapes.append(shape)
        return shapes

    @router.get("/models")
    async def models(request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"data": _public_model_shapes(request)}

    @router.get("/models/count")
    async def models_count(request: Request) -> dict[str, dict[str, int]]:
        return {"data": {"count": len(_public_model_shapes(request))}}

    @router.get("/models/user")
    async def models_user(_principal: ManagementPrincipal) -> dict[str, list[dict[str, Any]]]:
        return {"data": _public_model_shapes()}

    @router.get("/models/{author}/{slug}/endpoints")
    async def model_endpoints(
        author: str,
        slug: str,
        request: Request,
    ) -> dict[str, list[dict[str, Any]]]:
        model_id = f"{author}/{slug}"
        model = MODELS.get(model_id)
        if model is None:
            return {"data": []}
        prefs = provider_route_preferences(_provider_query_body(request))
        return {
            "data": [
                {
                    "name": PROVIDERS[endpoint.provider].name,
                    "provider_name": PROVIDERS[endpoint.provider].name,
                    "endpoint_id": endpoint.id,
                    "provider": endpoint.provider,
                    "context_length": model.context_length,
                    "pricing": {
                        "prompt": microdollars_per_million_tokens_to_token_decimal(
                            endpoint.prompt_price_microdollars_per_million_tokens
                        ),
                        "completion": microdollars_per_million_tokens_to_token_decimal(
                            endpoint.completion_price_microdollars_per_million_tokens
                        ),
                    },
                    "usage_type": endpoint.usage_type,
                    "upstream_id": endpoint.upstream_id,
                    "prompt_price_microdollars_per_million_tokens": (
                        endpoint.prompt_price_microdollars_per_million_tokens
                    ),
                    "completion_price_microdollars_per_million_tokens": (
                        endpoint.completion_price_microdollars_per_million_tokens
                    ),
                    "supported_parameters": [
                        "messages",
                        "temperature",
                        "top_p",
                        "max_tokens",
                        "stream",
                    ],
                    "trustedrouter": {
                        "attested_gateway": PROVIDERS[endpoint.provider].attested_gateway,
                        "stores_content": PROVIDERS[endpoint.provider].stores_content,
                        "provider_zero_data_retention": endpoint_zero_data_retention(endpoint),
                        "zero_data_retention_scope": endpoint_zero_data_retention_scope(endpoint),
                        "privacy_tier": endpoint_privacy_tier(endpoint),
                        "privacy_tier_label": PRIVACY_TIER_LABELS[endpoint_privacy_tier(endpoint)],
                        "provider_confidential_compute": PROVIDERS[
                            endpoint.provider
                        ].provider_confidential_compute,
                        "provider_e2ee": PROVIDERS[endpoint.provider].provider_e2ee,
                        "provider_headquarters_country": PROVIDERS[
                            endpoint.provider
                        ].provider_headquarters_country,
                        "provider_us_based": (
                            PROVIDERS[endpoint.provider].provider_headquarters_country
                            == PROVIDER_JURISDICTION_US
                        ),
                        "provider_eu_focused": endpoint.provider in EU_FOCUSED_PROVIDER_ORDER,
                        "provider_policy": model_provider_policy(
                            endpoint.model_id,
                            endpoint.provider,
                        ),
                        "provider_policy_url": model_provider_policy_url(
                            endpoint.model_id,
                            endpoint.provider,
                        ),
                        "usage_type": endpoint.usage_type,
                        "prepaid_available": endpoint.usage_type == "Credits",
                        "byok_available": endpoint.usage_type == "BYOK",
                    },
                }
                for _model, endpoint in catalog_endpoint_candidates(model, prefs)
            ]
        }

    @router.get("/endpoints/zdr")
    async def endpoints_zdr() -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [
                {
                    "provider": provider.slug,
                    "provider_name": provider.name,
                    "data_collection": "deny",
                    "attested_gateway": provider.attested_gateway,
                    "stores_content": provider.stores_content,
                    "provider_zero_data_retention": provider.provider_zero_data_retention,
                    "prepaid_zero_data_retention": provider.prepaid_zero_data_retention,
                    "prepaid_zero_data_retention_effective_on": (
                        provider.prepaid_zero_data_retention_effective_on
                    ),
                    "zero_data_retention_scope": (
                        "trustedrouter_prepaid"
                        if provider.prepaid_zero_data_retention
                        else "provider"
                        if provider.provider_zero_data_retention is True
                        else None
                    ),
                    "provider_confidential_compute": provider.provider_confidential_compute,
                    "provider_e2ee": provider.provider_e2ee,
                    "provider_policy": provider.provider_policy,
                    "provider_policy_url": provider.provider_policy_url,
                }
                for provider in providers_for_display()
                if provider.provider_zero_data_retention is True
                or provider.prepaid_zero_data_retention
                or provider.provider_confidential_compute is True
                or provider.provider_e2ee is True
            ]
        }

    @router.get("/regions")
    async def regions(settings: SettingsDep) -> dict[str, Any]:
        return {
            "data": region_payload(settings),
            "trustedrouter": {
                "multi_region_enabled": settings.multi_region_enabled,
                "primary_region": choose_region(settings),
            },
        }

    @router.get("/providers")
    async def providers() -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [provider_to_openrouter_shape(provider) for provider in providers_for_display()]
        }
