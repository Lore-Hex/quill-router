from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from trusted_router.catalog import (
    MODELS,
    endpoints_for_model,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.config import Settings
from trusted_router.dashboard import docs_llms_full_txt
from trusted_router.storage import STORE
from trusted_router.typed_balance import live_credit_summary

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_CHAT_TOKENS = 512


def register_mcp_routes(app: FastAPI, settings: Settings) -> None:
    server = TrustedRouterMCP(settings)

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse(_mcp_error(None, -32700, "Parse error"), status_code=400)
        response = await server.handle(payload, request)
        if response is None:
            return Response(status_code=204)
        return JSONResponse(response)


class TrustedRouterMCP:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._handlers: dict[str, Callable[[dict[str, Any], Request], Awaitable[Any]]] = {
            "ping": self._tool_ping,
            "models-list": self._tool_models_list,
            "model-get": self._tool_model_get,
            "model-endpoints": self._tool_model_endpoints,
            "providers-list": self._tool_providers_list,
            "credits-get": self._tool_credits_get,
            "generation-get": self._tool_generation_get,
            "docs-search": self._tool_docs_search,
            "chat-send": self._tool_chat_send,
        }

    async def handle(self, payload: Any, request: Request) -> Any:
        if isinstance(payload, list):
            responses = []
            for item in payload:
                response = await self.handle(item, request)
                if response is not None:
                    responses.append(response)
            return responses or None
        if not isinstance(payload, dict):
            return _mcp_error(None, -32600, "Invalid Request")
        request_id = payload.get("id")
        method = str(payload.get("method") or "")
        raw_params = payload.get("params")
        params = cast(dict[str, Any], raw_params) if isinstance(raw_params, dict) else {}
        if not request_id and method.startswith("notifications/"):
            return None
        if method == "initialize":
            return _mcp_result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "trustedrouter",
                        "version": self.settings.release,
                    },
                },
            )
        if method == "tools/list":
            return _mcp_result(request_id, {"tools": _mcp_tools()})
        if method == "tools/call":
            try:
                result = await self._call_tool(params, request)
            except MCPToolError as exc:
                result = _tool_text(exc.message, is_error=True)
            except Exception:
                result = _tool_text("TrustedRouter MCP tool failed", is_error=True)
            return _mcp_result(request_id, result)
        return _mcp_error(request_id, -32601, f"Method not found: {method}")

    async def _call_tool(self, params: dict[str, Any], request: Request) -> dict[str, Any]:
        name = str(params.get("name") or "")
        raw_args = params.get("arguments")
        args = cast(dict[str, Any], raw_args) if isinstance(raw_args, dict) else {}
        handler = self._handlers.get(name)
        if handler is None:
            raise MCPToolError(f"Unknown tool: {name}")
        return await handler(args, request)

    async def _tool_ping(self, _args: dict[str, Any], _request: Request) -> dict[str, Any]:
        return _tool_json(
            {
                "status": "ok",
                "api_base_url": self.settings.api_base_url,
                "docs": f"https://{self.settings.trusted_domain}/docs/mcp",
            }
        )

    async def _tool_models_list(self, args: dict[str, Any], _request: Request) -> dict[str, Any]:
        query = str(args.get("query") or "").strip().lower()
        limit = _bounded_int(args.get("limit"), default=25, minimum=1, maximum=100)
        models = [model_to_openrouter_shape(model) for model in MODELS.values()]
        if query:
            models = [
                item
                for item in models
                if query in str(item.get("id", "")).lower()
                or query in str(item.get("name", "")).lower()
                or query in str(item.get("description", "")).lower()
            ]
        models.sort(key=lambda item: str(item.get("id", "")))
        return _tool_json({"data": models[:limit], "total_matches": len(models)})

    async def _tool_model_get(self, args: dict[str, Any], _request: Request) -> dict[str, Any]:
        model_id = _required_string(args, "model")
        model = MODELS.get(model_id)
        if model is None:
            raise MCPToolError(f"Unknown model: {model_id}")
        return _tool_json({"data": model_to_openrouter_shape(model)})

    async def _tool_model_endpoints(
        self, args: dict[str, Any], _request: Request
    ) -> dict[str, Any]:
        model_id = _required_string(args, "model")
        if model_id not in MODELS:
            raise MCPToolError(f"Unknown model: {model_id}")
        return _tool_json(
            {
                "data": [
                    {
                        "id": endpoint.id,
                        "provider": endpoint.provider,
                        "usage_type": endpoint.usage_type,
                        "upstream_id": endpoint.upstream_id,
                        "prompt_price_microdollars_per_million_tokens": endpoint.prompt_price_microdollars_per_million_tokens,
                        "completion_price_microdollars_per_million_tokens": endpoint.completion_price_microdollars_per_million_tokens,
                    }
                    for endpoint in endpoints_for_model(model_id)
                ]
            }
        )

    async def _tool_providers_list(
        self, _args: dict[str, Any], _request: Request
    ) -> dict[str, Any]:
        return _tool_json(
            {"data": [provider_to_openrouter_shape(provider) for provider in providers_for_display()]}
        )

    async def _tool_credits_get(self, _args: dict[str, Any], request: Request) -> dict[str, Any]:
        bearer = _bearer_token(request)
        if not bearer:
            raise MCPToolError("credits-get requires Authorization: Bearer sk-tr-...")
        api_key = STORE.get_key_by_raw(bearer)
        if api_key is None or api_key.disabled:
            raise MCPToolError("Invalid TrustedRouter API key")
        summary = live_credit_summary(api_key.workspace_id)
        if summary is None:
            raise MCPToolError("No credit account found for this workspace")
        return _tool_json(
            {
                "data": {
                    "workspace_id": api_key.workspace_id,
                    "total_credits_microdollars": summary["total_credits"],
                    "total_usage_microdollars": summary["total_usage"],
                    "reserved_microdollars": summary["reserved"],
                    "available_microdollars": summary["available"],
                }
            }
        )

    async def _tool_generation_get(
        self, args: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        bearer = _bearer_token(request)
        if not bearer:
            raise MCPToolError("generation-get requires Authorization: Bearer sk-tr-...")
        api_key = STORE.get_key_by_raw(bearer)
        if api_key is None or api_key.disabled:
            raise MCPToolError("Invalid TrustedRouter API key")
        generation_id = _required_string(args, "id")
        generation = STORE.get_generation(generation_id)
        if generation is None or generation.workspace_id != api_key.workspace_id:
            raise MCPToolError(f"Unknown generation: {generation_id}")
        return _tool_json({"data": generation.to_openrouter_generation()})

    async def _tool_docs_search(self, args: dict[str, Any], _request: Request) -> dict[str, Any]:
        query = _required_string(args, "query").lower()
        limit = _bounded_int(args.get("limit"), default=5, minimum=1, maximum=10)
        docs = docs_llms_full_txt(self.settings)
        chunks = [chunk.strip() for chunk in docs.split("\n\n") if query in chunk.lower()]
        return _tool_json({"data": chunks[:limit], "total_matches": len(chunks)})

    async def _tool_chat_send(self, args: dict[str, Any], request: Request) -> dict[str, Any]:
        bearer = _bearer_token(request)
        if not bearer:
            raise MCPToolError("chat-send requires Authorization: Bearer sk-tr-...")
        model = _required_string(args, "model")
        message = _required_string(args, "message")
        max_tokens = _bounded_int(
            args.get("max_tokens"),
            default=min(MAX_MCP_CHAT_TOKENS, 128),
            minimum=1,
            maximum=MAX_MCP_CHAT_TOKENS,
        )
        body = {
            "model": model,
            "messages": [{"role": "user", "content": message}],
            "max_tokens": max_tokens,
            "stream": False,
        }
        timeout = httpx.Timeout(60.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.settings.api_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                    "X-Title": "TrustedRouter MCP",
                },
                json=body,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {"status_code": response.status_code, "text": response.text[:1000]}
        if response.status_code >= 400:
            raise MCPToolError(json.dumps(payload, sort_keys=True))
        return _tool_json({"data": payload})


class MCPToolError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _mcp_tools() -> list[dict[str, Any]]:
    return [
        _tool_schema("ping", "Health check for the TrustedRouter MCP server.", {}),
        _tool_schema(
            "models-list",
            "Search TrustedRouter's live model catalog.",
            {
                "query": {"type": "string", "optional": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "optional": True},
            },
        ),
        _tool_schema("model-get", "Get details for one model ID.", {"model": {"type": "string"}}),
        _tool_schema(
            "model-endpoints",
            "List providers/endpoints serving one model.",
            {"model": {"type": "string"}},
        ),
        _tool_schema("providers-list", "List TrustedRouter providers and privacy posture.", {}),
        _tool_schema("credits-get", "Get credit balance for the supplied API key.", {}),
        _tool_schema(
            "generation-get",
            "Get metadata for a generation ID.",
            {"id": {"type": "string"}},
        ),
        _tool_schema(
            "docs-search",
            "Search TrustedRouter documentation context.",
            {"query": {"type": "string"}, "limit": {"type": "integer", "optional": True}},
        ),
        _tool_schema(
            "chat-send",
            "Send one short test message through the attested API. This is billable.",
            {
                "model": {"type": "string"},
                "message": {"type": "string"},
                "max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_MCP_CHAT_TOKENS,
                    "optional": True,
                },
            },
            read_only=False,
        ),
    ]


def _tool_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    read_only: bool = True,
) -> dict[str, Any]:
    clean_properties = {
        key: {inner_key: inner_value for inner_key, inner_value in value.items() if inner_key != "optional"}
        for key, value in properties.items()
    }
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": clean_properties,
            "required": [
                key for key, value in properties.items() if not value.get("optional", False)
            ],
        },
        "annotations": {
            "readOnlyHint": read_only,
            "openWorldHint": False,
            "destructiveHint": not read_only,
        },
    }


def _mcp_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_json(payload: Any) -> dict[str, Any]:
    return _tool_text(json.dumps(payload, indent=2, sort_keys=True))


def _tool_text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _bearer_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _required_string(args: dict[str, Any], name: str) -> str:
    value = str(args.get(name) or "").strip()
    if not value:
        raise MCPToolError(f"{name} is required")
    return value


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
