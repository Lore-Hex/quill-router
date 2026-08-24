from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import is_api_key_expired
from trusted_router.catalog import (
    MODELS,
    endpoints_for_model,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.config import Settings
from trusted_router.dashboard import docs_llms_full_txt
from trusted_router.errors import error_response
from trusted_router.mcp_metadata import MCP_SERVER_NAME, MCP_SERVER_TITLE
from trusted_router.request_limits import enforce_authenticated_rate_limit
from trusted_router.storage import STORE, ApiKey
from trusted_router.typed_balance import live_credit_summary
from trusted_router.types import ErrorType

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_CHAT_TOKENS = 512
MAX_MCP_CHAT_MESSAGE_CHARS = 4_096
MAX_MCP_CHAT_MESSAGE_BYTES = 8_192
MAX_MCP_MODEL_CHARS = 256
MAX_MCP_MODEL_BYTES = 1_024
MAX_MCP_GENERATION_ID_CHARS = 128
MAX_MCP_GENERATION_ID_BYTES = 512
MAX_MCP_SEARCH_QUERY_CHARS = 256
MAX_MCP_SEARCH_QUERY_BYTES = 1_024
MAX_MCP_BATCH_ITEMS = 32
MAX_MCP_CHAT_BATCH_ITEMS = 1
MAX_MCP_EXPENSIVE_BATCH_ITEMS = 4
MAX_MCP_STORAGE_BATCH_ITEMS = 4
_EXPENSIVE_TOOLS = frozenset({"models-list", "model-endpoints", "providers-list", "docs-search"})
_STORAGE_TOOLS = frozenset({"credits-get", "generation-get"})
_MCP_AUTH_STATE_KEY = "trusted_router_mcp_auth"
_API_KEY_BEARER_PREFIX = "sk-tr-"


@dataclass(frozen=True)
class _MCPAuth:
    bearer: str
    api_key: ApiKey


def register_mcp_routes(app: FastAPI, settings: Settings) -> None:
    server = TrustedRouterMCP(settings)

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        try:
            await run_in_threadpool(server.require_api_key, request)
        except MCPToolError as exc:
            return error_response(exc.status_code, exc.message, exc.error_type)
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
        # Catalog and documentation inputs are immutable for the lifetime of a
        # deployed image. Build their JSON projections lazily so ordinary
        # control requests pay nothing, then reuse them across every MCP
        # request instead of letting a batch repeat full-catalog rendering.
        self._model_shapes: tuple[dict[str, object], ...] | None = None
        self._model_shapes_by_id: dict[str, dict[str, object]] | None = None
        self._provider_shapes: tuple[dict[str, object], ...] | None = None
        self._docs_chunks: tuple[str, ...] | None = None
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

    async def handle(self, payload: Any, request: Request, *, batch_depth: int = 0) -> Any:
        if batch_depth == 0:
            if isinstance(payload, list) and not payload:
                return _mcp_error(None, -32600, "JSON-RPC batch must not be empty")
            if isinstance(payload, list) and len(payload) > MAX_MCP_BATCH_ITEMS:
                return _mcp_error(
                    None,
                    -32600,
                    f"JSON-RPC batch exceeds the {MAX_MCP_BATCH_ITEMS}-item limit",
                )
            tool_names = _top_level_tool_names(payload)
            chat_calls = sum(name == "chat-send" for name in tool_names)
            if chat_calls > MAX_MCP_CHAT_BATCH_ITEMS:
                return _mcp_error(
                    None,
                    -32600,
                    "An MCP request may contain at most one billable chat-send call",
                )
            expensive_calls = sum(name in _EXPENSIVE_TOOLS for name in tool_names)
            if expensive_calls > MAX_MCP_EXPENSIVE_BATCH_ITEMS:
                return _mcp_error(
                    None,
                    -32600,
                    "An MCP request may contain at most "
                    f"{MAX_MCP_EXPENSIVE_BATCH_ITEMS} catalog or documentation calls",
                )
            storage_calls = sum(name in _STORAGE_TOOLS for name in tool_names)
            if storage_calls > MAX_MCP_STORAGE_BATCH_ITEMS:
                return _mcp_error(
                    None,
                    -32600,
                    "An MCP request may contain at most "
                    f"{MAX_MCP_STORAGE_BATCH_ITEMS} storage-backed calls",
                )
        if isinstance(payload, list):
            if batch_depth > 0:
                return _mcp_error(None, -32600, "Nested JSON-RPC batches are not supported")
            if not payload:
                return _mcp_error(None, -32600, "JSON-RPC batch must not be empty")
            if len(payload) > MAX_MCP_BATCH_ITEMS:
                return _mcp_error(
                    None,
                    -32600,
                    f"JSON-RPC batch exceeds the {MAX_MCP_BATCH_ITEMS}-item limit",
                )
            responses = []
            for item in payload:
                response = await self.handle(item, request, batch_depth=batch_depth + 1)
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
                        "name": MCP_SERVER_NAME,
                        "title": MCP_SERVER_TITLE,
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

    def require_api_key(self, request: Request) -> _MCPAuth:
        """Authenticate and rate-limit one HTTP request before decoding JSON-RPC."""
        return self._require_api_key(request)

    async def _tool_ping(self, _args: dict[str, Any], _request: Request) -> dict[str, Any]:
        return _tool_json(
            {
                "status": "ok",
                "api_base_url": self.settings.api_base_url,
                "docs": f"https://{self.settings.trusted_domain}/docs/mcp",
            }
        )

    async def _tool_models_list(self, args: dict[str, Any], _request: Request) -> dict[str, Any]:
        query = _bounded_string(
            args,
            "query",
            maximum_chars=MAX_MCP_SEARCH_QUERY_CHARS,
            maximum_bytes=MAX_MCP_SEARCH_QUERY_BYTES,
            required=False,
        ).lower()
        limit = _bounded_int(args.get("limit"), default=25, minimum=1, maximum=100)
        models = list(self._models_projection())
        if query:
            models = [
                item
                for item in models
                if query in str(item.get("id", "")).lower()
                or query in str(item.get("name", "")).lower()
                or query in str(item.get("description", "")).lower()
            ]
        return _tool_json({"data": models[:limit], "total_matches": len(models)})

    async def _tool_model_get(self, args: dict[str, Any], _request: Request) -> dict[str, Any]:
        model_id = _bounded_string(
            args,
            "model",
            maximum_chars=MAX_MCP_MODEL_CHARS,
            maximum_bytes=MAX_MCP_MODEL_BYTES,
        )
        self._models_projection()
        assert self._model_shapes_by_id is not None
        shape = self._model_shapes_by_id.get(model_id)
        if shape is None:
            raise MCPToolError(f"Unknown model: {model_id}")
        return _tool_json({"data": shape})

    async def _tool_model_endpoints(
        self, args: dict[str, Any], _request: Request
    ) -> dict[str, Any]:
        model_id = _bounded_string(
            args,
            "model",
            maximum_chars=MAX_MCP_MODEL_CHARS,
            maximum_bytes=MAX_MCP_MODEL_BYTES,
        )
        model = MODELS.get(model_id)
        # Same visibility rule as list/get: an internal-only model must not be
        # confirmable through its endpoints either.
        self._models_projection()
        assert self._model_shapes_by_id is not None
        if model is None or model_id not in self._model_shapes_by_id:
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
        return _tool_json({"data": self._providers_projection()})

    async def _tool_credits_get(self, _args: dict[str, Any], request: Request) -> dict[str, Any]:
        api_key = self._require_api_key(request).api_key
        summary = await run_in_threadpool(live_credit_summary, api_key.workspace_id)
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

    async def _tool_generation_get(self, args: dict[str, Any], request: Request) -> dict[str, Any]:
        api_key = self._require_api_key(request).api_key
        generation_id = _bounded_string(
            args,
            "id",
            maximum_chars=MAX_MCP_GENERATION_ID_CHARS,
            maximum_bytes=MAX_MCP_GENERATION_ID_BYTES,
        )
        generation = await run_in_threadpool(STORE.get_generation, generation_id)
        if generation is None or generation.workspace_id != api_key.workspace_id:
            raise MCPToolError(f"Unknown generation: {generation_id}")
        return _tool_json({"data": generation.to_openrouter_generation()})

    async def _tool_docs_search(self, args: dict[str, Any], _request: Request) -> dict[str, Any]:
        query = _bounded_string(
            args,
            "query",
            maximum_chars=MAX_MCP_SEARCH_QUERY_CHARS,
            maximum_bytes=MAX_MCP_SEARCH_QUERY_BYTES,
        ).lower()
        limit = _bounded_int(args.get("limit"), default=5, minimum=1, maximum=10)
        chunks = [chunk for chunk in self._documentation_chunks() if query in chunk.lower()]
        return _tool_json({"data": chunks[:limit], "total_matches": len(chunks)})

    def _models_projection(self) -> tuple[dict[str, object], ...]:
        if self._model_shapes is None:
            visible: list[dict[str, object]] = []
            by_id: dict[str, dict[str, object]] = {}
            for model in MODELS.values():
                shape = model_to_openrouter_shape(model)
                if _is_internal_model_shape(shape):
                    continue
                visible.append(shape)
                by_id[str(shape.get("id") or model.id)] = shape
            visible.sort(key=lambda item: str(item.get("id", "")))
            self._model_shapes = tuple(visible)
            self._model_shapes_by_id = by_id
        return self._model_shapes

    def _providers_projection(self) -> tuple[dict[str, object], ...]:
        if self._provider_shapes is None:
            self._provider_shapes = tuple(
                provider_to_openrouter_shape(provider) for provider in providers_for_display()
            )
        return self._provider_shapes

    def _documentation_chunks(self) -> tuple[str, ...]:
        if self._docs_chunks is None:
            self._docs_chunks = tuple(
                chunk.strip()
                for chunk in docs_llms_full_txt(self.settings).split("\n\n")
                if chunk.strip()
            )
        return self._docs_chunks

    async def _tool_chat_send(self, args: dict[str, Any], request: Request) -> dict[str, Any]:
        bearer = self._require_api_key(request).bearer
        model = _bounded_string(
            args,
            "model",
            maximum_chars=MAX_MCP_MODEL_CHARS,
            maximum_bytes=MAX_MCP_MODEL_BYTES,
        )
        message = _bounded_chat_message(args)
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

    def _require_api_key(self, request: Request) -> _MCPAuth:
        cached = getattr(request.state, _MCP_AUTH_STATE_KEY, None)
        if isinstance(cached, _MCPAuth):
            return cached
        if isinstance(cached, MCPToolError):
            raise cached

        bearer = _bearer_token(request)
        if not bearer:
            error = MCPToolError(
                "MCP requires Authorization: Bearer sk-tr-...",
                status_code=401,
                error_type=ErrorType.UNAUTHORIZED,
            )
            setattr(request.state, _MCP_AUTH_STATE_KEY, error)
            raise error
        if not _is_api_key_bearer(bearer):
            # Reject session tokens, provider keys, and other attacker-chosen
            # bearer shapes before hashing them or consulting remote storage.
            error = MCPToolError(
                "Invalid TrustedRouter API key",
                status_code=401,
                error_type=ErrorType.UNAUTHORIZED,
            )
            setattr(request.state, _MCP_AUTH_STATE_KEY, error)
            raise error
        try:
            context = STORE.api_key_auth_context(bearer)
        except Exception:  # noqa: BLE001 - cache fail-closed auth for the whole batch.
            error = MCPToolError(
                "TrustedRouter API key authentication is unavailable",
                status_code=503,
                error_type=ErrorType.SERVICE_UNAVAILABLE,
            )
            setattr(request.state, _MCP_AUTH_STATE_KEY, error)
            raise error from None
        if context is None:
            error = MCPToolError(
                "Invalid TrustedRouter API key",
                status_code=401,
                error_type=ErrorType.UNAUTHORIZED,
            )
            setattr(request.state, _MCP_AUTH_STATE_KEY, error)
            raise error
        api_key = context.api_key
        if api_key.disabled or is_api_key_expired(api_key.expires_at) or context.workspace is None:
            error = MCPToolError(
                "Invalid TrustedRouter API key",
                status_code=401,
                error_type=ErrorType.UNAUTHORIZED,
            )
            setattr(request.state, _MCP_AUTH_STATE_KEY, error)
            raise error
        enforce_authenticated_rate_limit(
            request,
            self.settings,
            credential_kind="api_key",
            stable_subject=api_key.lookup_hash,
        )
        auth = _MCPAuth(bearer=bearer, api_key=api_key)
        setattr(request.state, _MCP_AUTH_STATE_KEY, auth)
        return auth


class MCPToolError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        error_type: str = ErrorType.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


def _mcp_tools() -> list[dict[str, Any]]:
    return [
        _tool_schema("ping", "Health check for the TrustedRouter MCP server.", {}),
        _tool_schema(
            "models-list",
            "Search TrustedRouter's live model catalog.",
            {
                "query": {
                    "type": "string",
                    "maxLength": MAX_MCP_SEARCH_QUERY_CHARS,
                    "optional": True,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "optional": True},
            },
        ),
        _tool_schema(
            "model-get",
            "Get details for one model ID.",
            {"model": {"type": "string", "maxLength": MAX_MCP_MODEL_CHARS}},
        ),
        _tool_schema(
            "model-endpoints",
            "List providers/endpoints serving one model.",
            {"model": {"type": "string", "maxLength": MAX_MCP_MODEL_CHARS}},
        ),
        _tool_schema("providers-list", "List TrustedRouter providers and privacy posture.", {}),
        _tool_schema("credits-get", "Get credit balance for the supplied API key.", {}),
        _tool_schema(
            "generation-get",
            "Get metadata for a generation ID.",
            {"id": {"type": "string", "maxLength": MAX_MCP_GENERATION_ID_CHARS}},
        ),
        _tool_schema(
            "docs-search",
            "Search TrustedRouter documentation context.",
            {
                "query": {
                    "type": "string",
                    "maxLength": MAX_MCP_SEARCH_QUERY_CHARS,
                },
                "limit": {"type": "integer", "optional": True},
            },
        ),
        _tool_schema(
            "chat-send",
            "Send one short test message through the attested API. This is billable.",
            {
                "model": {"type": "string", "maxLength": MAX_MCP_MODEL_CHARS},
                "message": {
                    "type": "string",
                    "maxLength": MAX_MCP_CHAT_MESSAGE_CHARS,
                },
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
        key: {
            inner_key: inner_value
            for inner_key, inner_value in value.items()
            if inner_key != "optional"
        }
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


def _is_internal_model_shape(shape: dict[str, Any]) -> bool:
    trustedrouter = shape.get("trustedrouter")
    return bool(isinstance(trustedrouter, dict) and trustedrouter.get("internal_only"))


def _tool_text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _bearer_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _is_api_key_bearer(bearer: str) -> bool:
    """Recognize the same versioned API-key family used by normal auth."""
    return bearer.startswith(_API_KEY_BEARER_PREFIX) and len(bearer) > len(_API_KEY_BEARER_PREFIX)


def _top_level_tool_names(payload: Any) -> list[str]:
    """Inspect one JSON-RPC envelope without recursively walking attacker data."""
    items = payload if isinstance(payload, list) else [payload]
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("method") != "tools/call":
            continue
        params = item.get("params")
        if isinstance(params, dict):
            names.append(str(params.get("name") or ""))
    return names


def _bounded_string(
    args: dict[str, Any],
    name: str,
    *,
    maximum_chars: int,
    maximum_bytes: int,
    required: bool = True,
) -> str:
    if name not in args:
        if required:
            raise MCPToolError(f"{name} is required")
        return ""
    raw = args[name]
    if not isinstance(raw, str):
        raise MCPToolError(f"{name} must be a string")
    try:
        encoded_length = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raise MCPToolError(f"{name} must contain valid UTF-8") from None
    if len(raw) > maximum_chars or encoded_length > maximum_bytes:
        raise MCPToolError(
            f"{name} exceeds the MCP input limit "
            f"({maximum_chars} characters / {maximum_bytes} UTF-8 bytes)"
        )
    value = raw.strip()
    if required and not value:
        raise MCPToolError(f"{name} is required")
    return value


def _bounded_chat_message(args: dict[str, Any]) -> str:
    raw = args.get("message")
    if not isinstance(raw, str) or not raw.strip():
        raise MCPToolError("message is required")
    if (
        len(raw) > MAX_MCP_CHAT_MESSAGE_CHARS
        or len(raw.encode("utf-8")) > MAX_MCP_CHAT_MESSAGE_BYTES
    ):
        raise MCPToolError(
            "message exceeds the MCP chat input limit "
            f"({MAX_MCP_CHAT_MESSAGE_CHARS} characters / "
            f"{MAX_MCP_CHAT_MESSAGE_BYTES} UTF-8 bytes)"
        )
    return raw.strip()


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
