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
    endpoint_provider_policy,
    endpoint_provider_policy_url,
    endpoint_stores_content,
    endpoint_zero_data_retention,
    endpoint_zero_data_retention_scope,
    endpoints_for_model,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.catalog_aliases import is_listing_alias
from trusted_router.catalog_capabilities import (
    provider_extension_parameters,
    union_supported_parameters,
)
from trusted_router.chat_capabilities import reasoning_modes
from trusted_router.image_generation import (
    IMAGE_MODEL_ID_SET,
    OPENAI_IMAGE_MODEL_IDS,
    image_input_modalities,
    image_pricing_by_resolution,
    image_supported_parameters,
)
from trusted_router.model_aliases import canonical_router_model_id
from trusted_router.model_changes import last_route_changes, timestamp
from trusted_router.money import microdollars_per_million_tokens_to_token_decimal
from trusted_router.openai_service_tiers import (
    OPENAI_SERVICE_TIERS,
    openai_priority_pricing,
)
from trusted_router.provider_lifecycle import (
    _utc_now,
    provider_catalog_revision,
    provider_pricing_schedule,
)
from trusted_router.provider_locations import inference_location_metadata
from trusted_router.provider_precision import endpoint_precision_metadata, endpoint_quantization
from trusted_router.public_openapi import inference_servers
from trusted_router.regions import choose_region, region_payload
from trusted_router.request_capabilities import endpoint_capabilities
from trusted_router.routes.model_changes import register_model_change_routes
from trusted_router.routing import catalog_endpoint_candidates, provider_route_preferences

_PUBLIC_CATALOG_CACHE_CONTROL = "public, max-age=300, s-maxage=300, stale-while-revalidate=60"

# Per-route detail that the default GET /v1/models list leaves out. On
# 2026-10-10 the list was 4.25 MB for 656 models, and trustedrouter.endpoints
# was 2.87 MB of it. Clients that cap the decoded catalog at 4 MiB (OpenClaw)
# reported TrustedRouter unavailable. provider_policy is provider prose
# repeated on every row of that provider; its URL stays in the list.
# ?include=endpoints restores both, byte for byte, and
# GET /v1/models/{author}/{slug}/endpoints serves one model's routes.
_LIST_OMITTED_TRUSTEDROUTER_FIELDS = frozenset({"endpoints", "provider_policy"})
_INCLUDE_ENDPOINTS = "endpoints"


@dataclass(frozen=True)
class _PublicCatalogPayload:
    # Complete rows, for in-process readers and ?include=endpoints.
    shapes: tuple[dict[str, Any], ...]
    # Default list representation, without per-route detail.
    body: bytes
    etag: str
    gzip_body: bytes
    gzip_etag: str
    # ?include=endpoints representation: the complete rows.
    full_body: bytes
    full_etag: str
    full_gzip_body: bytes
    full_gzip_etag: str
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


def _list_model_shape(shape: dict[str, Any]) -> dict[str, Any]:
    trustedrouter = shape.get("trustedrouter")
    if not isinstance(trustedrouter, dict):
        return shape
    return {
        **shape,
        "trustedrouter": {
            key: value
            for key, value in trustedrouter.items()
            if key not in _LIST_OMITTED_TRUSTEDROUTER_FIELDS
        },
    }


def _includes_endpoints(request: Request) -> bool:
    return any(
        token.strip().lower() == _INCLUDE_ENDPOINTS
        for raw in request.query_params.getlist("include")
        for token in raw.split(",")
    )


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
def _public_catalog_payload(revision: tuple[int, str]) -> _PublicCatalogPayload:
    shapes: list[dict[str, Any]] = []
    last_changes = last_route_changes(timestamp(_utc_now()))
    for model in MODELS.values():
        if is_listing_alias(model.id, MODELS):
            continue
        shape = model_to_openrouter_shape(model)
        trustedrouter = shape.get("trustedrouter")
        if isinstance(trustedrouter, dict) and trustedrouter.get("internal_only"):
            continue
        if isinstance(trustedrouter, dict):
            trustedrouter["last_route_change_at"] = last_changes.get(model.id)
        shapes.append(shape)
    frozen_shapes = tuple(shapes)
    full_body = _json_bytes({"data": frozen_shapes})
    body = _json_bytes({"data": [_list_model_shape(shape) for shape in frozen_shapes]})
    picker_body = _json_bytes({"data": [_picker_model_shape(shape) for shape in frozen_shapes]})
    gzip_body = gzip.compress(body, compresslevel=6, mtime=0)
    full_gzip_body = gzip.compress(full_body, compresslevel=6, mtime=0)
    picker_gzip_body = gzip.compress(picker_body, compresslevel=6, mtime=0)
    return _PublicCatalogPayload(
        shapes=frozen_shapes,
        body=body,
        etag=_content_etag(body),
        gzip_body=gzip_body,
        gzip_etag=_content_etag(gzip_body),
        full_body=full_body,
        full_etag=_content_etag(full_body),
        full_gzip_body=full_gzip_body,
        full_gzip_etag=_content_etag(full_gzip_body),
        picker_body=picker_body,
        picker_etag=_content_etag(picker_body),
        picker_gzip_body=picker_gzip_body,
        picker_gzip_etag=_content_etag(picker_gzip_body),
    )


def _current_catalog_payload() -> _PublicCatalogPayload:
    return _public_catalog_payload(provider_catalog_revision())


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
    from trusted_router.speech import SPEECH_MODELS
    from trusted_router.transcription import TRANSCRIPTION_MODELS
    from trusted_router.web_fetch import FETCH_MODEL_ID, fetch_pricing

    if endpoint.model_id == FETCH_MODEL_ID:
        return fetch_pricing()

    if transcription := TRANSCRIPTION_MODELS.get(endpoint.model_id):
        return transcription.pricing
    if (spec := SPEECH_MODELS.get(endpoint.model_id)) and not spec.token_billed:
        return spec.non_token_pricing
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
    requested_parameters = {
        parameter.strip()
        for raw in request.query_params.getlist("supported_parameters")
        for parameter in raw.split(",")
        if parameter.strip()
    }
    supported_parameters = {
        str(parameter) for parameter in (shape.get("supported_parameters") or [])
    }
    if not requested_parameters.issubset(supported_parameters):
        return False
    requested_output_modalities = {
        modality.strip().lower()
        for raw in request.query_params.getlist("output_modalities")
        for modality in raw.split(",")
        if modality.strip()
    }
    if requested_output_modalities:
        if "image" in requested_output_modalities and not endpoints_for_model(
            str(shape.get("id") or "")
        ):
            return False
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
            "supported_parameters",
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
        "allowed_passthrough_parameters": ["moderation"] if model.id in OPENAI_IMAGE_MODEL_IDS else [],
        "supports_streaming": False,
        "pricing": image_pricing_by_resolution(
            model.id,
            endpoint.prompt_price_microdollars_per_million_tokens,
            endpoint.completion_price_microdollars_per_million_tokens,
            endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
            if endpoint.price_tiers else None,
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
            "provider_policy": endpoint_provider_policy(endpoint),
            "provider_policy_url": endpoint_provider_policy_url(endpoint),
            "usage_type": endpoint.usage_type,
            "prepaid_available": endpoint.usage_type == "Credits",
            "byok_available": endpoint.usage_type == "BYOK",
        },
    }


def register_catalog_routes(router: APIRouter) -> None:
    register_model_change_routes(router)
    # Prewarm the projection. Scheduled retirements/prices can change without
    # a release, so handlers retrieve the cached current revision, not a closure
    # over startup prices. Ordinary requests still share the prebuilt payload.
    _current_catalog_payload()

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
        catalog_payload = _current_catalog_payload()
        if request is None:
            return list(catalog_payload.shapes)
        return [
            shape
            for shape in catalog_payload.shapes
            if _public_model_matches_filters(shape, request)
        ]

    @router.get(
        "/models",
        response_model=dict[str, list[dict[str, Any]]],
        summary="List public models",
        description=(
            "Returns the live public model catalog. No API key is required. "
            "The canonical production URL is https://api.trustedrouter.com/v1/models. "
            "trustedrouter.capabilities agrees with model discovery declarations: "
            "tools, seed, vision (image input), and confidential booleans, plus reasoning_effort. "
            "tools and seed reflect the model's supported_parameters; vision reflects "
            "architecture.input_modalities. Some routes declare less than the model supports. "
            "reasoning_effort is the ordered union of verified values, [] when every route "
            "is verified to reject effort, otherwise null. null means not verified; [] means "
            "do not send reasoning_effort. Default routing filters by tools, tool_choice and "
            "response_format, which decide what kind of response comes back: it skips a route "
            "the catalog says lacks the capability and answers 400 naming the parameter when "
            "no route is left. Other parameters are filtered only with "
            "provider.require_parameters: true, which keeps only routes whose own "
            "supported_parameters include every parameter sent. Check the per-route values "
            "first: trustedrouter.endpoints[].capabilities (with ?include=endpoints) or "
            "trustedrouter.capabilities from GET /v1/models/{author}/{slug}/endpoints, "
            "and the route's supported_parameters. "
            "This filters parameter names, not effort values or image input; pin a suitable "
            "provider when values differ. Confidential routing requires "
            "provider.min_privacy: \"confidential\". Routing aliases omit the object. "
            "A routing pool such as trustedrouter/auto lists a parameter in supported_parameters "
            "only when every model it can route to has a pool route that accepts it, and lists "
            "the input modalities any of its routes accepts; routing sends image input only to "
            "routes that accept images. Its context_length is the largest window among the "
            "models it routes to; chat routing sends a request only to models whose window "
            "holds the estimated prompt plus max_tokens. "
            "The list omits per-route detail: trustedrouter.endpoints and the model-level "
            "trustedrouter.provider_policy text (trustedrouter.provider_policy_url remains). "
            "Add ?include=endpoints for rows with both. "
            "See https://trustedrouter.com/docs#model-capabilities for scope and routing rules."
        ),
        openapi_extra={
            "servers": inference_servers(),
            "parameters": [
                {
                    "name": "include",
                    "in": "query",
                    "required": False,
                    "description": (
                        "endpoints: also return trustedrouter.endpoints and "
                        "trustedrouter.provider_policy for every model."
                    ),
                    "schema": {"type": "string", "enum": [_INCLUDE_ENDPOINTS]},
                }
            ],
        },
    )
    async def models(request: Request) -> Response:
        catalog_payload = _current_catalog_payload()
        include_endpoints = _includes_endpoints(request)
        if not _has_public_model_filters(request):
            if include_endpoints:
                return _cached_json_response(
                    request,
                    catalog_payload.full_body,
                    catalog_payload.full_etag,
                    gzip_body=catalog_payload.full_gzip_body,
                    gzip_etag=catalog_payload.full_gzip_etag,
                )
            return _cached_json_response(
                request,
                catalog_payload.body,
                catalog_payload.etag,
                gzip_body=catalog_payload.gzip_body,
                gzip_etag=catalog_payload.gzip_etag,
            )
        shapes = _public_model_shapes(request)
        if not include_endpoints:
            shapes = [_list_model_shape(shape) for shape in shapes]
        body = _json_bytes({"data": shapes})
        compressed = gzip.compress(body, compresslevel=6, mtime=0)
        return _cached_json_response(
            request,
            body,
            _content_etag(body),
            gzip_body=compressed,
            gzip_etag=_content_etag(compressed),
        )

    @router.get("/models/count")
    async def models_count(request: Request, response: Response) -> dict[str, dict[str, int]]:
        # Anonymous public catalogue data, readable by static exchange sites.
        response.headers["Access-Control-Allow-Origin"] = "*"
        return {"data": {"count": len(_public_model_shapes(request))}}

    @router.get("/models/picker")
    async def models_picker(request: Request) -> Response:
        catalog_payload = _current_catalog_payload()
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
        model_id = canonical_router_model_id(f"{author}/{slug}")
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
                    "context_length": endpoint.effective_context_length(_model),
                    "pricing": _endpoint_pricing_payload(endpoint),
                    "usage_type": endpoint.usage_type,
                    "upstream_id": endpoint.upstream_id,
                    "quantization": endpoint_quantization(endpoint),
                    "prompt_price_microdollars_per_million_tokens": (
                        endpoint.prompt_price_microdollars_per_million_tokens
                    ),
                    "completion_price_microdollars_per_million_tokens": (
                        endpoint.completion_price_microdollars_per_million_tokens
                    ),
                    "supported_parameters": list(
                        union_supported_parameters(
                            endpoint.supported_parameters,
                            provider_extension_parameters(endpoint.provider),
                        )
                    ),
                    "trustedrouter": {
                        "capabilities": endpoint_capabilities(_model, endpoint),
                        "precision": endpoint_precision_metadata(endpoint),
                        "reasoning_modes": reasoning_modes(endpoint.provider, endpoint.model_id),
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
                        "inference_location": inference_location_metadata(endpoint.provider, endpoint.model_id),
                        # The remaining per-route facts from the list's
                        # trustedrouter.endpoints, which the default list omits.
                        "renewable_energy_inference": PROVIDERS[
                            endpoint.provider
                        ].renewable_energy_inference,
                        "energy_policy_url": PROVIDERS[endpoint.provider].energy_policy_url,
                        "request_price_microdollars": endpoint.request_price_microdollars,
                        "provider_policy": endpoint_provider_policy(endpoint),
                        "provider_policy_url": endpoint_provider_policy_url(endpoint),
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
    async def providers(response: Response) -> dict[str, list[dict[str, Any]]]:
        # No credentials are needed or allowed for cross-origin catalogue reads.
        response.headers["Access-Control-Allow-Origin"] = "*"
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
        return {"data": list(_current_catalog_payload().shapes)}
