from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.catalog import (
    MODELS,
    PROVIDERS,
    endpoints_for_model,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.regions import choose_region, region_payload


def register_catalog_routes(router: APIRouter) -> None:
    @router.get("/embeddings/models")
    async def embeddings_models() -> dict[str, list[dict[str, Any]]]:
        return {"data": [model_to_openrouter_shape(m) for m in MODELS.values() if m.supports_embeddings]}

    def _public_model_shapes() -> list[dict[str, Any]]:
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
            shapes.append(shape)
        return shapes

    @router.get("/models")
    async def models() -> dict[str, list[dict[str, Any]]]:
        return {"data": _public_model_shapes()}

    @router.get("/models/count")
    async def models_count() -> dict[str, dict[str, int]]:
        return {"data": {"count": len(_public_model_shapes())}}

    @router.get("/models/user")
    async def models_user(_principal: ManagementPrincipal) -> dict[str, list[dict[str, Any]]]:
        return {"data": _public_model_shapes()}

    @router.get("/models/{author}/{slug}/endpoints")
    async def model_endpoints(author: str, slug: str) -> dict[str, list[dict[str, Any]]]:
        model_id = f"{author}/{slug}"
        model = MODELS.get(model_id)
        if model is None:
            return {"data": []}
        pricing = model_to_openrouter_shape(model)["pricing"]
        return {
            "data": [
                {
                    "name": PROVIDERS[endpoint.provider].name,
                    "provider_name": PROVIDERS[endpoint.provider].name,
                    "endpoint_id": endpoint.id,
                    "provider": endpoint.provider,
                    "context_length": model.context_length,
                    "pricing": pricing,
                    "supported_parameters": ["messages", "temperature", "top_p", "max_tokens", "stream"],
                    "trustedrouter": {
                        "attested_gateway": PROVIDERS[endpoint.provider].attested_gateway,
                        "stores_content": PROVIDERS[endpoint.provider].stores_content,
                        "provider_zero_data_retention": PROVIDERS[
                            endpoint.provider
                        ].provider_zero_data_retention,
                        "provider_confidential_compute": PROVIDERS[
                            endpoint.provider
                        ].provider_confidential_compute,
                        "provider_e2ee": PROVIDERS[endpoint.provider].provider_e2ee,
                        "provider_policy": PROVIDERS[endpoint.provider].provider_policy,
                        "provider_policy_url": PROVIDERS[endpoint.provider].provider_policy_url,
                        "usage_type": endpoint.usage_type,
                        "prepaid_available": endpoint.usage_type == "Credits",
                        "byok_available": endpoint.usage_type == "BYOK",
                    },
                }
                for endpoint in endpoints_for_model(model.id)
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
                    "provider_confidential_compute": provider.provider_confidential_compute,
                    "provider_e2ee": provider.provider_e2ee,
                    "provider_policy": provider.provider_policy,
                    "provider_policy_url": provider.provider_policy_url,
                }
                for provider in providers_for_display()
                if provider.provider_zero_data_retention is True
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
        return {"data": [provider_to_openrouter_shape(provider) for provider in providers_for_display()]}
