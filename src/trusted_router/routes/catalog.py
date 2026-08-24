from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.catalog import (
    EU_FOCUSED_PROVIDER_ORDER,
    MODELS,
    PRIVACY_TIER_LABELS,
    PROVIDER_JURISDICTION_US,
    PROVIDERS,
    ModelEndpoint,
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoint_privacy_tier,
    endpoint_stores_content,
    endpoint_zero_data_retention,
    endpoint_zero_data_retention_scope,
    endpoints_for_model,
    model_provider_policy,
    model_provider_policy_url,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.image_generation import (
    IMAGE_MODEL_ID_SET,
    image_input_modalities,
    image_pricing_by_resolution,
    image_supported_parameters,
)
from trusted_router.money import microdollars_per_million_tokens_to_token_decimal
from trusted_router.openai_service_tiers import (
    OPENAI_SERVICE_TIERS,
    openai_priority_pricing,
)
from trusted_router.provider_lifecycle import provider_pricing_schedule
from trusted_router.regions import choose_region, region_payload
from trusted_router.routing import catalog_endpoint_candidates, provider_route_preferences

_PUBLIC_CATALOG_CACHE_CONTROL = "public, max-age=300, s-maxage=300, stale-while-revalidate=60"


@dataclass(frozen=True)
class _PublicCatalogPayload:
    shapes: tuple[dict[str, Any], ...]
    body: bytes
    etag: str
    gzip_body: bytes
    gzip_etag: str
    picker_body: bytes
    picker_etag: str
    picker_gzip_body: bytes
    picker_gzip_etag: str


def _json_bytes(data: object) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _content_etag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'


def _weak_etag_value(value: str) -> str:
    value = value.strip()
    return value[2:].lstrip() if value.startswith("W/") else value


def _picker_model_shape(shape: dict[str, Any]) -> dict[str, Any]:
    pricing = shape.get("pricing")
    trustedrouter = shape.get("trustedrouter")
    pricing = pricing if isinstance(pricing, dict) else {}
    trustedrouter = trustedrouter if isinstance(trustedrouter, dict) else {}
    return {
        "id": shape.get("id"),
        "name": shape.get("name"),
        "description": shape.get("description"),
        "context_length": shape.get("context_length"),
        "pricing": {
            "prompt": pricing.get("prompt"),
            "completion": pricing.get("completion"),
        },
        "trustedrouter": {
            "capabilities": trustedrouter.get("capabilities", []),
            "uptime_pct": trustedrouter.get("uptime_pct"),
            "open_weights": trustedrouter.get("open_weights", False),
            "us_provider_available": trustedrouter.get("us_provider_available", False),
            "eu_focused_provider_available": trustedrouter.get(
                "eu_focused_provider_available", False
            ),
            "internal_only": trustedrouter.get("internal_only", False),
            "route_kind": trustedrouter.get("route_kind", "model"),
            "supports_chat": trustedrouter.get("supports_chat", True),
        },
    }


@lru_cache(maxsize=1)
def _public_catalog_payload() -> _PublicCatalogPayload:
    shapes: list[dict[str, Any]] = []
    for model in MODELS.values():
        shape = model_to_openrouter_shape(model)
        trustedrouter = shape.get("trustedrouter")
        if isinstance(trustedrouter, dict) and trustedrouter.get("internal_only"):
            continue
        shapes.append(shape)
    frozen_shapes = tuple(shapes)
    body = _json_bytes({"data": frozen_shapes})
    picker_body = _json_bytes({"data": [_picker_model_shape(shape) for shape in frozen_shapes]})
    gzip_body = gzip.compress(body, compresslevel=6, mtime=0)
    picker_gzip_body = gzip.compress(picker_body, compresslevel=6, mtime=0)
    return _PublicCatalogPayload(
        shapes=frozen_shapes,
        body=body,
        etag=_content_etag(body),
        gzip_body=gzip_body,
        gzip_etag=_content_etag(gzip_body),
        picker_body=picker_body,
        picker_etag=_content_etag(picker_body),
        picker_gzip_body=picker_gzip_body,
        picker_gzip_etag=_content_etag(picker_gzip_body),
    )


def _cached_json_response(
    request: Request,
    body: bytes,
    etag: str,
    *,
    gzip_body: bytes | None = None,
    gzip_etag: str | None = None,
) -> Response:
    accept_encoding = request.headers.get("accept-encoding", "")
    accepted_encodings: dict[str, float] = {}
    for entry in accept_encoding.split(","):
        token, *parameters = entry.split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
                if not 0.0 <= quality <= 1.0:
                    quality = 0.0
        accepted_encodings[token.strip().lower()] = quality
    explicit_gzip_qualities = [
        accepted_encodings[coding] for coding in ("gzip", "x-gzip") if coding in accepted_encodings
    ]
    gzip_quality = (
        max(explicit_gzip_qualities)
        if explicit_gzip_qualities
        else accepted_encodings.get("*", 0.0)
    )
    serve_gzip = gzip_body is not None and gzip_quality > 0
    if serve_gzip:
        assert gzip_body is not None
        body = gzip_body
        etag = gzip_etag or _content_etag(body)
    headers = {
        "Cache-Control": _PUBLIC_CATALOG_CACHE_CONTROL,
        "ETag": etag,
    }
    if serve_gzip:
        headers["Content-Encoding"] = "gzip"
    elif "gzip" in accept_encoding:
        # Starlette's outer GZipMiddleware only checks whether the token
        # "gzip" occurs in Accept-Encoding, so values such as x-gzip or an
        # explicit q=0 rejection can otherwise make it compress our identity
        # bytes after we have assigned their strong ETag.
        headers["Content-Encoding"] = "identity"
    validators = {
        _weak_etag_value(token) for token in request.headers.get("if-none-match", "").split(",")
    }
    not_modified = "*" in validators or _weak_etag_value(etag) in validators
    if serve_gzip or "Content-Encoding" in headers or not_modified:
        # The outer compression middleware adds this for a normal identity
        # response. Set it ourselves when the body already carries an encoding,
        # and on 304 where the middleware has no body from which to infer it.
        headers["Vary"] = "Accept-Encoding"
    if not_modified:
        return Response(status_code=304, headers=headers)
    return Response(
        content=body,
        media_type="application/json",
        headers=headers,
    )


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


def _endpoint_supported_parameters(provider: str) -> list[str]:
    parameters = [
        "messages",
        "temperature",
        "top_p",
        "max_tokens",
        "stream",
    ]
    if provider == "openai":
        parameters.append("service_tier")
    return parameters


def _openai_service_tier_metadata(provider: str, model_id: str) -> dict[str, Any]:
    if provider != "openai":
        return {}
    if pricing := openai_priority_pricing(model_id):
        return {
            "service_tiers": list(OPENAI_SERVICE_TIERS),
            "priority_pricing": pricing.public_payload(),
        }
    return {"service_tiers": ["default"]}


def _endpoint_pricing_payload(endpoint: ModelEndpoint) -> dict[str, str]:
    payload = {
        "prompt": microdollars_per_million_tokens_to_token_decimal(
            endpoint.prompt_price_microdollars_per_million_tokens
        ),
        "completion": microdollars_per_million_tokens_to_token_decimal(
            endpoint.completion_price_microdollars_per_million_tokens
        ),
    }
    tiers = getattr(endpoint, "price_tiers", ()) or ()
    cached_price = tiers[0].prompt_cached_price_microdollars_per_million_tokens if tiers else None
    if cached_price is not None:
        payload["input_cache_read"] = microdollars_per_million_tokens_to_token_decimal(cached_price)
    return payload


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
    requested_output_modalities = {
        modality.strip().lower()
        for raw in request.query_params.getlist("output_modalities")
        for modality in raw.split(",")
        if modality.strip()
    }
    if requested_output_modalities:
        architecture = shape.get("architecture")
        output_modalities = (
            architecture.get("output_modalities", []) if isinstance(architecture, dict) else []
        )
        if not requested_output_modalities.issubset(
            {str(modality).lower() for modality in output_modalities}
        ):
            return False
    return True


def _has_public_model_filters(request: Request) -> bool:
    return any(
        key in request.query_params
        for key in (
            "open_weights",
            "provider[jurisdiction]",
            "provider.jurisdiction",
            "provider[region]",
            "provider.region",
            "region",
            "output_modalities",
        )
    )


def _image_model_shape(model: Any) -> dict[str, Any]:
    shape = model_to_openrouter_shape(model)
    raw_architecture = shape.get("architecture")
    architecture = dict(raw_architecture) if isinstance(raw_architecture, dict) else {}
    architecture["input_modalities"] = image_input_modalities(model.id)
    architecture["output_modalities"] = ["image"]
    architecture["modality"] = f"{'+'.join(architecture['input_modalities'])}->image"
    return {
        "id": shape["id"],
        "name": shape["name"],
        "description": shape["description"],
        "created": shape["created"],
        "architecture": architecture,
        "pricing": shape["pricing"],
        "supported_parameters": image_supported_parameters(model.id),
        "supports_streaming": False,
        "endpoints": f"/v1/images/models/{model.id}/endpoints",
        "trustedrouter": shape["trustedrouter"],
    }


def _image_endpoint_shape(model: Any, endpoint: ModelEndpoint) -> dict[str, Any]:
    provider = PROVIDERS[endpoint.provider]
    return {
        "provider_name": provider.name,
        "provider_slug": endpoint.provider,
        "provider_tag": endpoint.provider,
        "supported_parameters": image_supported_parameters(model.id),
        "allowed_passthrough_parameters": [],
        "supports_streaming": False,
        "pricing": image_pricing_by_resolution(
            model.id,
            endpoint.prompt_price_microdollars_per_million_tokens,
            endpoint.completion_price_microdollars_per_million_tokens,
        ),
        "trustedrouter": {
            "attested_gateway": provider.attested_gateway,
            "stores_content": endpoint_stores_content(endpoint),
            "provider_zero_data_retention": endpoint_zero_data_retention(endpoint),
            "zero_data_retention_scope": endpoint_zero_data_retention_scope(endpoint),
            "privacy_tier": endpoint_privacy_tier(endpoint),
            "privacy_tier_label": PRIVACY_TIER_LABELS[endpoint_privacy_tier(endpoint)],
            "provider_confidential_compute": endpoint_confidential_compute(endpoint),
            "provider_e2ee": endpoint_e2ee(endpoint),
            "provider_policy": model_provider_policy(model.id, endpoint.provider),
            "provider_policy_url": model_provider_policy_url(model.id, endpoint.provider),
            "usage_type": endpoint.usage_type,
            "prepaid_available": endpoint.usage_type == "Credits",
            "byok_available": endpoint.usage_type == "BYOK",
        },
    }


def register_catalog_routes(router: APIRouter) -> None:
    # Catalog inputs are immutable for the life of a release. Pay the expensive
    # endpoint/policy projection once while the app is starting, rather than on
    # the first request handled by a shared event loop.
    catalog_payload = _public_catalog_payload()

    @router.get("/embeddings/models")
    async def embeddings_models() -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [model_to_openrouter_shape(m) for m in MODELS.values() if m.supports_embeddings]
        }

    @router.get("/images/models")
    async def image_models() -> dict[str, list[dict[str, Any]]]:
        return {
            "data": [
                _image_model_shape(model)
                for model in MODELS.values()
                if model.id in IMAGE_MODEL_ID_SET and endpoints_for_model(model.id)
            ]
        }

    @router.get("/images/models/{author}/{slug}/endpoints")
    async def image_model_endpoints(
        author: str,
        slug: str,
        request: Request,
    ) -> dict[str, Any]:
        model_id = f"{author}/{slug}"
        model = MODELS.get(model_id)
        if model is None or model.id not in IMAGE_MODEL_ID_SET:
            return {"id": model_id, "endpoints": []}
        prefs = provider_route_preferences(_provider_query_body(request))
        return {
            "id": model.id,
            "endpoints": [
                _image_endpoint_shape(model, endpoint)
                for _candidate_model, endpoint in catalog_endpoint_candidates(model, prefs)
            ],
        }

    def _public_model_shapes(request: Request | None = None) -> list[dict[str, Any]]:
        # `internal_only` models (e.g. trustedrouter/monitor) must
        # never appear in the public catalog — they're system-internal
        # routing pools, not user-selectable. The shape itself carries
        # the flag; filter it BEFORE handing to callers so SDKs +
        # chat playground don't accidentally surface them.
        if request is None:
            return list(catalog_payload.shapes)
        return [
            shape
            for shape in catalog_payload.shapes
            if _public_model_matches_filters(shape, request)
        ]

    @router.get("/models", response_model=dict[str, list[dict[str, Any]]])
    async def models(request: Request) -> Response:
        if not _has_public_model_filters(request):
            return _cached_json_response(
                request,
                catalog_payload.body,
                catalog_payload.etag,
                gzip_body=catalog_payload.gzip_body,
                gzip_etag=catalog_payload.gzip_etag,
            )
        body = _json_bytes({"data": _public_model_shapes(request)})
        compressed = gzip.compress(body, compresslevel=6, mtime=0)
        return _cached_json_response(
            request,
            body,
            _content_etag(body),
            gzip_body=compressed,
            gzip_etag=_content_etag(compressed),
        )

    @router.get("/models/count")
    async def models_count(request: Request) -> dict[str, dict[str, int]]:
        return {"data": {"count": len(_public_model_shapes(request))}}

    @router.get("/models/picker")
    async def models_picker(request: Request) -> Response:
        return _cached_json_response(
            request,
            catalog_payload.picker_body,
            catalog_payload.picker_etag,
            gzip_body=catalog_payload.picker_gzip_body,
            gzip_etag=catalog_payload.picker_gzip_etag,
        )

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
                    "pricing": _endpoint_pricing_payload(endpoint),
                    "usage_type": endpoint.usage_type,
                    "upstream_id": endpoint.upstream_id,
                    "prompt_price_microdollars_per_million_tokens": (
                        endpoint.prompt_price_microdollars_per_million_tokens
                    ),
                    "completion_price_microdollars_per_million_tokens": (
                        endpoint.completion_price_microdollars_per_million_tokens
                    ),
                    "supported_parameters": _endpoint_supported_parameters(endpoint.provider),
                    "trustedrouter": {
                        "attested_gateway": PROVIDERS[endpoint.provider].attested_gateway,
                        "stores_content": endpoint_stores_content(endpoint),
                        "provider_zero_data_retention": endpoint_zero_data_retention(endpoint),
                        "zero_data_retention_scope": endpoint_zero_data_retention_scope(endpoint),
                        "privacy_tier": endpoint_privacy_tier(endpoint),
                        "privacy_tier_label": PRIVACY_TIER_LABELS[endpoint_privacy_tier(endpoint)],
                        "provider_confidential_compute": endpoint_confidential_compute(endpoint),
                        "provider_e2ee": endpoint_e2ee(endpoint),
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
                        **_openai_service_tier_metadata(
                            endpoint.provider,
                            endpoint.model_id,
                        ),
                        "pricing_schedule": provider_pricing_schedule(
                            endpoint.provider,
                            endpoint.model_id,
                        ),
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


def register_authenticated_catalog_routes(router: APIRouter) -> None:
    """Register catalog views that require a management principal.

    Keeping this route out of :func:`register_catalog_routes` lets the public
    service expose immutable catalog data without also mounting an auth-backed
    endpoint on the anonymous failure domain.
    """

    @router.get("/models/user")
    async def models_user(
        _principal: ManagementPrincipal,
    ) -> dict[str, list[dict[str, Any]]]:
        return {"data": list(_public_catalog_payload().shapes)}
