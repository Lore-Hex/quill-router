"""Inference HTTP routes: chat/completions, messages, responses, embeddings.

Lives separately from main.py because:
  * The chat handler is the longest single route on TR — when it was
    inline in main.py it was a 100-line block with two near-duplicate
    JSONResponse builders.
  * Validation helpers (`_validate_chat_messages`, `_require_chat_model`,
    `_require_messages_model`) live here so they're co-located with
    the only callers.
  * Response-envelope shaping is factored: `_chat_completion_envelope`,
    `_anthropic_messages_envelope`, and `_responses_api_envelope` each
    own one OpenRouter-/OpenAI-/Anthropic-shaped reply schema.

main.py owns app creation + middleware wiring + non-inference routes;
this module owns the inference dispatch logic.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from trusted_router.adapter import (
    messages_to_chat_body,
    resolve_max_output_tokens,
    responses_to_chat_body,
)
from trusted_router.auth import (
    InferencePrincipal,
    Principal,
    SettingsDep,
)
from trusted_router.catalog import AUTO_MODEL_ID, MODELS, MONITOR_MODEL_ID, Model
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.routes.helpers import json_body
from trusted_router.routing import (
    chat_route_candidates,
    chat_route_endpoint_candidates,
    provider_route_preferences,
    resolve_model_alias,
)
from trusted_router.security import lookup_hash_api_key
from trusted_router.services.inference import (
    run_chat,
    run_chat_candidates,
    run_chat_candidates_stream,
    run_chat_stream,
    run_embeddings,
    run_messages_stream,
)
from trusted_router.types import ErrorType, UsageType

_VALID_ROLES = frozenset({"system", "user", "assistant", "tool", "developer"})
_OUTPUT_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


# ---------------------------------------------------------------------------
# Public registration entrypoint
# ---------------------------------------------------------------------------


def register_inference_routes(router: APIRouter) -> None:
    """Attach `/chat/completions`, `/messages`, `/responses`,
    `/embeddings` to the given inference router. Caller decides whether
    that router is mounted on the app (production) or not (control-plane
    inference is gated to local/test only — see
    `_control_plane_inference_enabled`)."""

    @router.post("/chat/completions")
    async def chat_completions(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> Any:
        body = await json_body(request)
        _validate_output_token_limit(body)
        _validate_chat_messages(body)
        _require_monitor_model_key(body, principal, settings)
        provider_prefs = provider_route_preferences(body)
        usage_type = (
            UsageType.coerce(provider_prefs.usage_type)
            if provider_prefs.usage_type
            else None
        )
        if usage_type is None:
            candidates = chat_route_candidates(body, settings)
        else:
            candidates = [
                model for model, _ep in chat_route_endpoint_candidates(body, settings)
            ]
        requested_model = str(body.get("model") or (body.get("models") or [""])[0])
        is_meta_route = len(candidates) > 1 or requested_model == AUTO_MODEL_ID
        app_name = _app_name(request)

        if is_meta_route:
            if body.get("stream") is True:
                return StreamingResponse(
                    _candidate_stream_bytes(
                        body,
                        candidates,
                        requested_model=requested_model,
                        principal=principal,
                        settings=settings,
                        app_name=app_name,
                        usage_type=usage_type,
                    ),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
                )
            result, generation, selected_model, failures = await run_chat_candidates(
                body,
                candidates,
                principal,
                settings,
                app_name=app_name,
                usage_type=usage_type,
            )
            return JSONResponse(
                _chat_completion_envelope(
                    result=result,
                    model_id=selected_model.id,
                    generation_id=generation.id,
                    generation=generation,
                    extra_tr_block={
                        "requested_model": requested_model,
                        "selected_model": selected_model.id,
                        "rollover_failures": failures,
                    },
                )
            )

        # Single-candidate path.
        model = candidates[0]
        # Surface routing-provenance headers so non-streaming clients
        # AND streaming clients can show "served by …" without parsing
        # the SSE wire. The provider on a single-candidate request is
        # decided up front (no rollover), so emitting on the response
        # header is correct.
        provenance_headers = {
            "x-trustedrouter-provider": model.provider,
            "x-trustedrouter-served-model": model.id,
        }
        if body.get("stream") is True:
            return StreamingResponse(
                run_chat_stream(
                    body,
                    model,
                    principal,
                    settings,
                    app_name=app_name,
                    usage_type=usage_type,
                ),
                media_type="text/event-stream",
                headers={
                    "cache-control": "no-cache",
                    "x-accel-buffering": "no",
                    **provenance_headers,
                },
            )
        result, generation = await run_chat(
            body,
            model,
            principal,
            settings,
            app_name=app_name,
            usage_type=usage_type,
        )
        return JSONResponse(
            _chat_completion_envelope(
                result=result,
                model_id=model.id,
                generation_id=generation.id,
                generation=generation,
                extra_tr_block={"selected_provider": model.provider},
            ),
            headers=provenance_headers,
        )

    @router.post("/messages")
    async def messages(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> Any:
        body = await json_body(request)
        _validate_output_token_limit(body)
        model = _require_messages_model(body)
        chat_body = messages_to_chat_body(body, model_id=model.id)
        app_name = _app_name(request)
        if body.get("stream") is True:
            return StreamingResponse(
                run_messages_stream(
                    chat_body,
                    model,
                    principal,
                    settings,
                    app_name=app_name,
                ),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        result, generation = await run_chat(
            chat_body, model, principal, settings, app_name=app_name
        )
        return JSONResponse(
            _anthropic_messages_envelope(
                result=result,
                model_id=model.id,
                generation_id=generation.id,
            )
        )

    @router.post("/embeddings")
    async def embeddings(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        body = await json_body(request)
        model = _require_embeddings_model(body)
        result, generation = await run_embeddings(
            body,
            model,
            principal,
            settings,
            app_name=_app_name(request),
        )
        # The provider envelope is already OpenAI-shaped; attach the TR
        # provenance block (mirrors chat) and surface routing headers.
        envelope = dict(result)
        envelope["trustedrouter"] = {
            "generation_id": generation.id,
            "content_stored": False,
            "selected_provider": model.provider,
        }
        return JSONResponse(
            envelope,
            headers={
                "x-trustedrouter-provider": model.provider,
                "x-trustedrouter-served-model": model.id,
            },
        )

    @router.post("/responses")
    async def responses(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        body = await json_body(request)
        _validate_output_token_limit(body)
        chat_body = responses_to_chat_body(body)
        _require_monitor_model_key(chat_body, principal, settings)
        model = _require_chat_model(chat_body)
        result, generation = await run_chat(
            chat_body,
            model,
            principal,
            settings,
            app_name=_app_name(request),
        )
        return JSONResponse(
            _responses_api_envelope(
                result=result,
                model_id=model.id,
                generation_id=generation.id,
            )
        )


# ---------------------------------------------------------------------------
# Response envelopes — one per API surface we expose. Factored out so the
# JSON shape lives in one place and is easy to diff against the upstream
# spec when (e.g.) OpenAI adds a field.
# ---------------------------------------------------------------------------


def _chat_completion_envelope(
    *,
    result: Any,
    model_id: str,
    generation_id: str,
    generation: Any | None = None,
    extra_tr_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OpenAI / OpenRouter `/chat/completions` shape."""
    tr_block: dict[str, Any] = {
        "generation_id": generation_id,
        "content_stored": False,
    }
    if extra_tr_block:
        tr_block.update(extra_tr_block)
    usage: dict[str, Any] = {
        "prompt_tokens": result.input_tokens,
        "completion_tokens": result.output_tokens,
        "total_tokens": result.input_tokens + result.output_tokens,
    }
    cached_tokens = _known_positive_int(
        getattr(result, "cached_input_tokens", 0),
        getattr(generation, "cached_input_tokens", 0),
    )
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    reasoning_tokens = _known_positive_int(
        getattr(result, "reasoning_tokens", 0),
        getattr(generation, "reasoning_tokens", 0),
    )
    if reasoning_tokens:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    message_content = result.text
    message: dict[str, Any] = {"role": "assistant", "content": message_content}
    tool_calls = _known_tool_calls(
        getattr(result, "tool_calls", None),
        getattr(generation, "tool_calls", None),
    )
    if tool_calls:
        if not message_content:
            message["content"] = None
        message["tool_calls"] = tool_calls
    return {
        "id": result.request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": usage,
        "trustedrouter": tr_block,
    }


def _known_positive_int(*values: Any) -> int:
    for value in values:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def _known_tool_calls(*values: Any) -> list[dict[str, Any]] | None:
    for value in values:
        if not isinstance(value, list):
            continue
        tool_calls: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                tool_calls.append({str(key): item_value for key, item_value in item.items()})
        if tool_calls:
            return tool_calls
    return None


def _anthropic_messages_envelope(
    *,
    result: Any,
    model_id: str,
    generation_id: str,
) -> dict[str, Any]:
    """Anthropic `/v1/messages` shape."""
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": [{"type": "text", "text": result.text}],
        "stop_reason": result.finish_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        },
        "trustedrouter": {"generation_id": generation_id, "content_stored": False},
    }


def _responses_api_envelope(
    *,
    result: Any,
    model_id: str,
    generation_id: str,
) -> dict[str, Any]:
    """OpenAI `/v1/responses` shape."""
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": model_id,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": result.text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        },
        "trustedrouter": {"generation_id": generation_id, "content_stored": False},
    }


# ---------------------------------------------------------------------------
# Validators — co-located with the only callers (the route handlers above).
# ---------------------------------------------------------------------------


def _require_chat_model(body: dict[str, Any]) -> Model:
    model_id = str(body.get("model") or "")
    if not model_id:
        raise api_error(400, "model is required", ErrorType.BAD_REQUEST)
    # Accept bare/dated OpenAI-style ids (gpt-4.1, gpt-4.1-2025-04-14) on the
    # direct path, same as the routing resolver does for the gateway path.
    model_id = resolve_model_alias(model_id)
    model = MODELS.get(model_id)
    if model is None or not model.supports_chat:
        raise api_error(
            400, "Model does not support chat completions", ErrorType.MODEL_NOT_SUPPORTED
        )
    _validate_messages_field(body)
    return model


def _require_messages_model(body: dict[str, Any]) -> Model:
    model_id = str(body.get("model") or "")
    if not model_id:
        raise api_error(400, "model is required", ErrorType.BAD_REQUEST)
    model = MODELS.get(model_id)
    if model is None or not model.supports_messages:
        raise api_error(
            400,
            "Model does not support Anthropic Messages",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    return model


def _require_embeddings_model(body: dict[str, Any]) -> Model:
    model_id = str(body.get("model") or "")
    if not model_id:
        raise api_error(400, "model is required", ErrorType.BAD_REQUEST)
    model = MODELS.get(model_id)
    if model is None or not model.supports_embeddings:
        raise api_error(
            400, "Model does not support embeddings", ErrorType.MODEL_NOT_SUPPORTED
        )
    _validate_embeddings_input(body)
    return model


def _validate_embeddings_input(body: dict[str, Any]) -> None:
    """`input` must be a non-empty string or a non-empty list of strings.
    (Token-array inputs aren't supported yet — TR embeds text.)"""
    value = body.get("input")
    if isinstance(value, str):
        if not value:
            raise api_error(400, "input must not be empty", ErrorType.BAD_REQUEST)
        return
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item for item in value)
    ):
        return
    raise api_error(
        400, "input must be a non-empty string or array of strings", ErrorType.BAD_REQUEST
    )


def _validate_chat_messages(body: dict[str, Any]) -> None:
    """Validate body['messages'] for /chat/completions. Same shape check
    as `_require_chat_model` does internally; this is the standalone
    pre-route-resolution gate."""
    _validate_messages_field(body)


def _validate_output_token_limit(body: dict[str, Any]) -> None:
    field = next(
        (key for key in _OUTPUT_TOKEN_FIELDS if body.get(key) is not None),
        "max_tokens",
    )
    try:
        max_tokens = resolve_max_output_tokens(body)
    except (TypeError, ValueError) as exc:
        raise api_error(
            400,
            f"{field} must be an integer",
            ErrorType.BAD_REQUEST,
        ) from exc
    if max_tokens is not None and max_tokens < 1:
        raise api_error(400, f"{field} must be at least 1", ErrorType.BAD_REQUEST)
    if max_tokens is not None:
        body[field] = max_tokens


def _validate_messages_field(body: dict[str, Any]) -> None:
    """The single source of truth for chat-message shape validation.
    Replaces what used to be duplicated between `_require_chat_model`
    and `_validate_chat_messages` in main.py."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise api_error(
            400, "messages must contain at least one item", ErrorType.BAD_REQUEST
        )
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise api_error(
                400, f"messages[{index}] must be an object", ErrorType.BAD_REQUEST
            )
        if message.get("role") not in _VALID_ROLES:
            raise api_error(
                400, f"messages[{index}].role is unsupported", ErrorType.BAD_REQUEST
            )
        if "content" not in message:
            raise api_error(
                400, f"messages[{index}].content is required", ErrorType.BAD_REQUEST
            )


def _require_monitor_model_key(
    body: dict[str, Any],
    principal: Principal,
    settings: Settings,
) -> None:
    """Block any caller from requesting `trustedrouter/monitor` unless
    they hold the synthetic-monitor API key. The monitor model is for
    internal probing only; otherwise customers could hammer it for free
    routing decisions."""
    if not _requests_monitor_model(body):
        return
    api_key = principal.api_key
    expected = settings.synthetic_monitor_api_key
    if api_key is not None and expected and api_key.lookup_hash == lookup_hash_api_key(
        expected
    ):
        return
    raise api_error(
        403,
        "trustedrouter/monitor is restricted to the synthetic monitor key",
        ErrorType.FORBIDDEN,
    )


def _requests_monitor_model(body: dict[str, Any]) -> bool:
    if str(body.get("model") or "").strip() == MONITOR_MODEL_ID:
        return True
    models = body.get("models")
    if isinstance(models, list):
        return any(str(model).strip() == MONITOR_MODEL_ID for model in models)
    return False


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------


async def _candidate_stream_bytes(
    body: dict[str, Any],
    candidates: list[Model],
    *,
    requested_model: str,
    principal: Principal,
    settings: Settings,
    app_name: str,
    usage_type: UsageType | None = None,
) -> AsyncIterator[bytes]:
    """Streams chat-completions chunks for the meta-router path. The
    first chunk includes a `trustedrouter.route` SSE event identifying
    which candidate was selected, so SDK consumers can attribute the
    stream to a specific upstream model."""
    selected: str | None = None
    async for model, chunk in run_chat_candidates_stream(
        body,
        candidates,
        principal,
        settings,
        app_name=app_name,
        usage_type=usage_type,
    ):
        if selected is None:
            selected = model.id
            yield (
                "event: trustedrouter.route\n"
                f'data: {{"requested_model":"{requested_model}",'
                f'"selected_model":"{selected}"}}\n\n'
            ).encode()
        yield chunk


def _app_name(request: Request) -> str:
    return (
        request.headers.get("x-title")
        or request.headers.get("http-referer")
        or request.headers.get("referer")
        or "TrustedRouter"
    )
