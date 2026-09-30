"""Public, read-only MCP for the ChatGPT Model Advisor plugin.

This endpoint has no account, inference, arbitrary-URL, or storage tools. It
reuses the public catalog cache and request admission middleware, not /mcp's
authenticated dispatch table. Tool arguments are metadata, never prompts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool

from trusted_router.catalog import (
    MODELS,
    PROVIDERS,
    ModelEndpoint,
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoint_provider_policy,
    endpoint_provider_policy_url,
    endpoint_zero_data_retention,
    endpoint_zero_data_retention_scope,
    endpoints_for_model,
    provider_to_openrouter_shape,
)
from trusted_router.config import Settings
from trusted_router.dashboard import docs_llms_full_txt
from trusted_router.pricing import resolve_request_rates
from trusted_router.provider_locations import inference_location_metadata
from trusted_router.routes.catalog import _current_catalog_payload, _endpoint_pricing_payload

MAX_BODY_BYTES = 16_384
Privacy = Literal["any", "zdr", "confidential"]
ModelID = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9/_.:+-]+$")
]
Slug = Annotated[str, StringConstraints(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")]
Count = Annotated[int, Field(strict=True, ge=0, le=10_000_000)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Search(Arguments):
    query: str = Field(
        default="", max_length=120, description="Model name or family, not user content."
    )
    min_context: Count = 0
    privacy: Privacy = "any"
    limit: int = Field(default=10, ge=1, le=20)


class Compare(Arguments):
    models: list[ModelID] = Field(min_length=1, max_length=5)
    privacy: Privacy = "any"


class Estimate(Arguments):
    model: ModelID
    provider: Slug
    input_tokens: Count
    output_tokens: Count
    requests: int = Field(default=1, ge=1, le=1_000_000)


class ProviderLookup(Arguments):
    provider: Slug


class Docs(Arguments):
    query: str = Field(min_length=1, max_length=120, description="Documentation keywords only.")


TOOLS: dict[str, tuple[type[Arguments], str]] = {
    "search_models": (
        Search,
        "Search TrustedRouter's public model catalog by name, context, and route privacy. Returns at most 20 models, not a quality ranking. No account or model call.",
    ),
    "compare_models": (
        Compare,
        "Compare up to five exact catalog model IDs and their prepaid routes, prices, capabilities, privacy evidence and inference locations. Does not run an eval or verify live attestation.",
    ),
    "estimate_cost": (
        Estimate,
        "Estimate uncached text-token cost for one exact model/provider route using token counts and current retail rates, including context tiers. No inference or charge. Excludes tools, reasoning tokens not counted, media, signed receipts and other extras.",
    ),
    "get_provider": (
        ProviderLookup,
        "Read one provider's public privacy policy and catalog metadata. Company headquarters are not inference locations. ZDR is not a substitute for attestation.",
    ),
    "search_docs": (
        Docs,
        "Find public TrustedRouter API documentation by keywords. Returns short excerpts and documentation links. Does not access user documents.",
    ),
}


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": schema.model_json_schema(),
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
            "securitySchemes": [{"type": "noauth"}],
        }
        for name, (schema, description) in TOOLS.items()
    ]


def _shapes() -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in _current_catalog_payload().shapes}


def _routes(model_id: str, privacy: Privacy = "any") -> list[ModelEndpoint]:
    routes = [
        endpoint for endpoint in endpoints_for_model(model_id) if endpoint.usage_type == "Credits"
    ]
    if privacy != "any":
        routes = [endpoint for endpoint in routes if endpoint_zero_data_retention(endpoint) is True]
    if privacy == "confidential":
        routes = [
            endpoint
            for endpoint in routes
            if endpoint_confidential_compute(endpoint) is True and endpoint_e2ee(endpoint) is True
        ]
    return sorted(routes, key=lambda endpoint: (endpoint.provider, endpoint.id))


def _route_shape(endpoint: ModelEndpoint) -> dict[str, Any]:
    return {
        "provider": endpoint.provider,
        "endpoint_id": endpoint.id,
        "usage_type": endpoint.usage_type,
        "pricing_usd_per_token": _endpoint_pricing_payload(endpoint),
        "supported_parameters": list(endpoint.supported_parameters),
        "zero_data_retention": endpoint_zero_data_retention(endpoint),
        "zero_data_retention_scope": endpoint_zero_data_retention_scope(endpoint),
        "confidential_compute": endpoint_confidential_compute(endpoint),
        "e2ee": endpoint_e2ee(endpoint),
        "privacy_policy": endpoint_provider_policy(endpoint),
        "privacy_policy_url": endpoint_provider_policy_url(endpoint),
        "inference_location": inference_location_metadata(endpoint.provider, endpoint.model_id),
    }


def _model_shape(shape: dict[str, Any]) -> dict[str, Any]:
    return {
        key: shape.get(key)
        for key in ("id", "name", "context_length", "architecture", "supported_parameters")
    }


def _docs(settings: Settings) -> tuple[str, ...]:
    return tuple(
        part.strip() for part in docs_llms_full_txt(settings).split("\n\n") if part.strip()
    )


def _call_tool(name: str, raw: Any, settings: Settings) -> dict[str, Any]:
    definition = TOOLS.get(name)
    if definition is None:
        raise ValueError("Unknown read-only advisor tool")
    args = definition[0].model_validate(raw)
    if isinstance(args, ProviderLookup):
        provider = PROVIDERS.get(args.provider)
        if provider is None:
            raise ValueError("Unknown provider")
        return {
            "provider": provider_to_openrouter_shape(provider),
            "source": f"https://trustedrouter.com/providers/{args.provider}",
        }
    if isinstance(args, Docs):
        words = args.query.casefold().split()
        excerpts = [
            part for part in _docs(settings) if all(word in part.casefold() for word in words)
        ]
        return {
            "excerpts": [part[:2_000] for part in excerpts[:5]],
            "source": "https://trustedrouter.com/docs",
        }
    shapes = _shapes()
    if isinstance(args, Search):
        query = args.query.casefold()
        matches = []
        for model_id, shape in shapes.items():
            if query not in f"{model_id} {shape.get('name', '')}".casefold():
                continue
            if int(shape.get("context_length") or 0) < args.min_context:
                continue
            if args.privacy != "any" and not _routes(model_id, args.privacy):
                continue
            matches.append(shape)
        matches.sort(key=lambda row: str(row["id"]))
        return {
            "models": [_model_shape(row) for row in matches[: args.limit]],
            "total_matches": len(matches),
            "source": "https://trustedrouter.com/v1/models",
        }
    if isinstance(args, Compare):
        if any(model_id not in shapes for model_id in args.models):
            raise ValueError("Unknown or unavailable model; use search_models for current IDs")
        return {
            "models": [
                {
                    **_model_shape(shapes[model_id]),
                    "routes": [
                        _route_shape(endpoint) for endpoint in _routes(model_id, args.privacy)
                    ],
                    "source": f"https://trustedrouter.com/models/{model_id}",
                }
                for model_id in dict.fromkeys(args.models)
            ],
            "privacy_filter": args.privacy,
            "notice": "Catalog evidence, not a live attestation check. Empty routes means no matching prepaid route; never relax the requested privacy constraint.",
        }
    assert isinstance(args, Estimate)
    if args.model not in shapes:
        raise ValueError("Unknown or unavailable model")
    model = MODELS[args.model]
    if model.id.startswith("trustedrouter/") or not model.supports_chat:
        raise ValueError(
            "Estimates require a direct text model, not an alias, combo or media model"
        )
    if args.input_tokens + args.output_tokens > model.context_length:
        raise ValueError("Requested tokens exceed the model's catalog context limit")
    endpoints = [endpoint for endpoint in _routes(args.model) if endpoint.provider == args.provider]
    if len(endpoints) != 1:
        raise ValueError("An unambiguous prepaid route for that exact model/provider is required")
    endpoint = endpoints[0]
    rates = resolve_request_rates(
        endpoint.price_tiers,
        headline_prompt_micro_per_m=endpoint.prompt_price_microdollars_per_million_tokens,
        headline_completion_micro_per_m=endpoint.completion_price_microdollars_per_million_tokens,
        total_prompt_tokens=args.input_tokens,
    )
    token_micro = (
        args.input_tokens * rates.prompt_price_microdollars_per_million_tokens
        + args.output_tokens * rates.completion_price_microdollars_per_million_tokens
    )
    cost = Decimal(token_micro) / Decimal(1_000_000_000_000)
    return {
        "model": args.model,
        "provider": args.provider,
        "currency": "USD",
        "estimated_token_cost_per_request": str(cost),
        "requests": args.requests,
        "estimated_total_token_cost": str(cost * args.requests),
        "assumptions": "Uncached text tokens at current retail rates including the standard router fee. Token estimate only, not a quote. Excludes per-request fees, tools, media, extra reasoning tokens, signed receipts, retries, minimum billing rounding and future price changes.",
        "source": f"https://trustedrouter.com/v1/models/{args.model}/endpoints",
    }


def _handle(payload: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    request_id = payload.get("id")
    if (
        payload.get("jsonrpc") != "2.0"
        or not isinstance(payload.get("method"), str)
        or isinstance(request_id, bool)
        or (request_id is not None and not isinstance(request_id, (str, int)))
        or (isinstance(request_id, str) and len(request_id) > 128)
    ):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    method = payload["method"]
    if "id" not in payload:
        return None
    result: dict[str, Any]
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "trustedrouter-model-advisor",
                "title": "TrustedRouter Model Advisor",
                "version": "1.0.0",
            },
            "instructions": "Read-only public catalog tools. No credentials, private content, account changes or inference. Privacy claims describe the exact upstream route, not ChatGPT's processing of this conversation.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tool_list()}
    elif method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Invalid tool parameters"},
            }
        try:
            data = _call_tool(params["name"], params.get("arguments", {}), settings)
            data["checked_at"] = datetime.now(UTC).isoformat()
            result = {
                "content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data,
                "isError": False,
            }
        except (ValidationError, ValueError) as exc:
            # ValidationError embeds submitted values; never echo those to logs or output.
            message = (
                "Invalid tool arguments; follow the tool schema"
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            result = {"content": [{"type": "text", "text": message}], "isError": True}
        except Exception:
            result = {
                "content": [
                    {"type": "text", "text": "Public catalog temporarily unavailable; retry later"}
                ],
                "isError": True,
            }
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def register_advisor_mcp_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/.well-known/openai-apps-challenge", include_in_schema=False)
    async def openai_apps_challenge() -> Response:
        # Public ownership proof issued to Lore Hex Corp's Model Advisor draft.
        # This is not a credential. Keep it stable while that plugin is listed.
        return PlainTextResponse(
            "qYEFG9OeQ9eQ42p_rin3l4iW92CkpERa_z6blaw39_U",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.post("/mcp/advisor", include_in_schema=False)
    async def advisor_mcp(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin and origin not in {
            "https://chatgpt.com",
            "https://platform.openai.com",
            "https://trustedrouter.com",
        }:
            return Response(status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return JSONResponse({"error": "Advisor request too large"}, status_code=413)
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "One JSON-RPC request is required"},
                },
                status_code=400,
            )
        response = await run_in_threadpool(_handle, payload, settings)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response, headers={"Cache-Control": "no-store"})
