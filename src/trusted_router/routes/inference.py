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

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
from trusted_router.catalog import AUTO_MODEL_ID, MODELS, MONITOR_MODEL_ID, PROVIDERS, Model
from trusted_router.config import Settings
from trusted_router.custom_model_billing import (
    custom_model_cost_microdollars,
    owner_share_microdollars,
    user_model_payout_event_id,
)
from trusted_router.errors import api_error
from trusted_router.provider_types import (
    ProviderResult,
    estimate_tokens_from_messages,
    estimate_tokens_from_text,
)
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
from trusted_router.services.inference_quota import reserved_quota
from trusted_router.services.user_model_dispatch import (
    BufferedUserModelDispatch,
    dispatch_user_model,
    stream_user_model,
)
from trusted_router.storage import STORE, Generation
from trusted_router.storage_custom_models import (
    is_user_provided_model_id,
    normalize_user_provided_model_id,
)
from trusted_router.storage_models import UserProvidedModel
from trusted_router.types import ErrorType, UsageType
from trusted_router.user_model_rules import user_model_gateway_pair, user_model_is_on_the_clock

_VALID_ROLES = frozenset({"system", "user", "assistant", "tool", "developer"})
_OUTPUT_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
logger = logging.getLogger(__name__)


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
        user_model = _local_user_model_or_none(body)
        if user_model is not None:
            if body.get("stream") is True:
                return StreamingResponse(
                    await _prime_stream(
                        _stream_local_user_model(
                            user_model,
                            body,
                            principal,
                            settings,
                            app_name=_app_name(request),
                            request=request,
                        )
                    ),
                    media_type="text/event-stream",
                    headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
                )
            result = await _dispatch_local_user_model(
                user_model,
                body,
                principal,
                settings,
                app_name=_app_name(request),
                request=request,
            )
            return JSONResponse(result.body)
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
                    await _prime_stream(
                        _candidate_stream_bytes(
                            body,
                            candidates,
                            requested_model=requested_model,
                            principal=principal,
                            settings=settings,
                            app_name=app_name,
                            usage_type=usage_type,
                            request=request,
                        )
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
                request=request,
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
                await _prime_stream(
                    run_chat_stream(
                        body,
                        model,
                        principal,
                        settings,
                        app_name=app_name,
                        usage_type=usage_type,
                        request=request,
                    )
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
            request=request,
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
                await _prime_stream(
                    run_messages_stream(
                        chat_body,
                        model,
                        principal,
                        settings,
                        app_name=app_name,
                        request=request,
                    )
                ),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        result, generation = await run_chat(
            chat_body,
            model,
            principal,
            settings,
            app_name=app_name,
            request=request,
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
            request=request,
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
            request=request,
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


async def _dispatch_local_user_model(
    model: UserProvidedModel,
    body: dict[str, Any],
    principal: InferencePrincipal,
    settings: Settings,
    *,
    app_name: str,
    request: Request | None = None,
) -> BufferedUserModelDispatch:
    assert principal.api_key is not None
    sentinel, _endpoint = _local_user_model_pair(model)
    prompt_price = int(model.prompt_price_microdollars_per_million_tokens)
    completion_price = int(model.completion_price_microdollars_per_million_tokens)
    input_estimate = estimate_tokens_from_messages(body.get("messages", []))
    reserve_amount = custom_model_cost_microdollars(
        input_tokens=input_estimate,
        output_tokens=_local_max_output_tokens(body),
        prompt_price=prompt_price,
        completion_price=completion_price,
    )
    async with reserved_quota(
        principal,
        sentinel,
        reserve_amount=reserve_amount,
        input_tokens=input_estimate,
        streamed=False,
        region=settings.primary_region,
        usage_type_override=UsageType.CREDITS,
        request=request,
    ) as ticket:
        dispatch = await dispatch_user_model(model, body, settings)
        output_text = _owner_response_text(dispatch.body)
        usage = _sane_owner_usage(dispatch.body.get("usage"))
        input_tokens, output_tokens = (
            usage
            if usage is not None
            else (input_estimate, estimate_tokens_from_text(output_text))
        )
        actual_cost = _capped_user_model_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_price=prompt_price,
            completion_price=completion_price,
            hold=reserve_amount,
        )
        ticket.settle(actual_cost)
        choice = _owner_choice(dispatch.body)
        provider_result = ProviderResult(
            text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            provider_name=PROVIDERS["trustedrouter"].name,
            request_id=str(dispatch.body.get("id") or f"chatcmpl-{uuid.uuid4().hex}"),
            usage_estimated=usage is None,
            elapsed_seconds=dispatch.elapsed_seconds,
            first_token_seconds=dispatch.first_token_seconds,
        )
        _record_local_user_model_generation(
            model,
            principal,
            provider_result,
            actual_cost=actual_cost,
            streamed=False,
            app_name=app_name,
            region=settings.primary_region,
        )
        return dispatch


async def _stream_local_user_model(
    model: UserProvidedModel,
    body: dict[str, Any],
    principal: InferencePrincipal,
    settings: Settings,
    *,
    app_name: str,
    request: Request | None = None,
) -> AsyncIterator[bytes]:
    assert principal.api_key is not None
    sentinel, _endpoint = _local_user_model_pair(model)
    prompt_price = int(model.prompt_price_microdollars_per_million_tokens)
    completion_price = int(model.completion_price_microdollars_per_million_tokens)
    input_estimate = estimate_tokens_from_messages(body.get("messages", []))
    reserve_amount = custom_model_cost_microdollars(
        input_tokens=input_estimate,
        output_tokens=_local_max_output_tokens(body),
        prompt_price=prompt_price,
        completion_price=completion_price,
    )
    async with reserved_quota(
        principal,
        sentinel,
        reserve_amount=reserve_amount,
        input_tokens=input_estimate,
        streamed=True,
        region=settings.primary_region,
        usage_type_override=UsageType.CREDITS,
        request=request,
    ) as ticket:
        started_at = time.monotonic()
        text_parts: list[str] = []
        usage: tuple[int, int] | None = None
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        finish_reason = "stop"
        first_token_seconds: float | None = None
        owner_error = False
        saw_done = False
        async for chunk in stream_user_model(model, body, settings):
            if chunk.lstrip().startswith(b"event: error"):
                owner_error = True
            for data in _stream_data_payloads(chunk):
                if data == "[DONE]":
                    saw_done = True
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                request_id = str(event.get("id") or request_id)
                event_usage = _sane_owner_usage(event.get("usage"))
                if event_usage is not None:
                    usage = event_usage
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                delta = choice.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    content = delta["content"]
                    if content:
                        if first_token_seconds is None:
                            first_token_seconds = max(time.monotonic() - started_at, 0.001)
                        text_parts.append(content)
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice["finish_reason"])
            yield chunk
        if owner_error or not saw_done:
            return
        output_text = "".join(text_parts)
        input_tokens, output_tokens = (
            usage
            if usage is not None
            else (input_estimate, estimate_tokens_from_text(output_text))
        )
        actual_cost = _capped_user_model_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_price=prompt_price,
            completion_price=completion_price,
            hold=reserve_amount,
        )
        ticket.settle(actual_cost)
        provider_result = ProviderResult(
            text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
            provider_name=PROVIDERS["trustedrouter"].name,
            request_id=request_id,
            usage_estimated=usage is None,
            elapsed_seconds=max(time.monotonic() - started_at, 0.001),
            first_token_seconds=first_token_seconds,
        )
        _record_local_user_model_generation(
            model,
            principal,
            provider_result,
            actual_cost=actual_cost,
            streamed=True,
            app_name=app_name,
            region=settings.primary_region,
        )


def _local_user_model_pair(model: UserProvidedModel) -> tuple[Model, Any]:
    return user_model_gateway_pair(
        model_id=model.id,
        name=model.name,
        revision=model.revision,
        prompt_price_microdollars_per_m=(
            model.prompt_price_microdollars_per_million_tokens
        ),
        completion_price_microdollars_per_m=(
            model.completion_price_microdollars_per_million_tokens
        ),
        owner_user_id=model.owner_user_id,
        upstream_model_id=model.upstream_model_id,
    )


def _local_max_output_tokens(body: dict[str, Any]) -> int:
    value = resolve_max_output_tokens(body)
    return 512 if value is None else int(value)


def _capped_user_model_cost(
    model: UserProvidedModel,
    *,
    input_tokens: int,
    output_tokens: int,
    prompt_price: int,
    completion_price: int,
    hold: int,
) -> int:
    """Owner-priced cost, never above the hold the caller authorized.

    Same rule as the gateway settle: the token counts are the PAYEE's own
    meter, so the reservation (estimated prompt + max_output at frozen prices)
    is the ceiling on what the caller can be charged and the owner paid.
    """
    cost = custom_model_cost_microdollars(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        prompt_price=prompt_price,
        completion_price=completion_price,
    )
    if cost > hold:
        logger.warning(
            "billing.user_model_settle_capped_to_hold",
            extra={
                "user_provided_model_id": model.id,
                "reported_microdollars": cost,
                "hold_microdollars": hold,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
        return hold
    return cost


def _sane_owner_usage(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if (
        isinstance(prompt, bool)
        or isinstance(completion, bool)
        or not isinstance(prompt, int)
        or not isinstance(completion, int)
        or prompt < 0
        or completion < 0
    ):
        return None
    return prompt, completion


def _owner_choice(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def _owner_response_text(body: dict[str, Any]) -> str:
    message = _owner_choice(body).get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _stream_data_payloads(chunk: bytes) -> list[str]:
    return [
        line.removeprefix(b"data:").strip().decode("utf-8", errors="ignore")
        for line in chunk.splitlines()
        if line.startswith(b"data:")
    ]


def _record_local_user_model_generation(
    model: UserProvidedModel,
    principal: InferencePrincipal,
    result: ProviderResult,
    *,
    actual_cost: int,
    streamed: bool,
    app_name: str,
    region: str | None,
) -> Generation:
    assert principal.api_key is not None
    generation = Generation.from_chat_result(
        result=result,
        workspace_id=principal.workspace.id,
        key_hash=principal.api_key.hash,
        model_id=model.id,
        app_name=app_name,
        actual_cost_microdollars=actual_cost,
        usage_type=UsageType.CREDITS,
        streamed=streamed,
        provider="trustedrouter",
        region=region,
    )
    payout = owner_share_microdollars(actual_cost)
    generation.custom_model_id = model.id
    generation.operator_cost_microdollars = payout
    STORE.add_generation(generation)
    if payout > 0:
        try:
            STORE.credit_user_earnings(
                model.owner_user_id,
                payout,
                user_model_payout_event_id(generation.id),
                custom_model_id=model.id,
                payer_workspace_id=principal.workspace.id,
            )
        except Exception:
            logger.error(
                "user_model_payout_failed authorization_id=%s owner=%s",
                generation.id,
                model.owner_user_id,
                exc_info=True,
            )
    return generation


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


def _local_user_model_or_none(body: dict[str, Any]) -> UserProvidedModel | None:
    model_id = str(body.get("model") or "")
    if not is_user_provided_model_id(model_id):
        return None
    model = STORE.get_user_model(normalize_user_provided_model_id(model_id))
    if model is None:
        return None
    if not model.enabled or model.status != "active":
        raise api_error(404, "Custom model not found", ErrorType.NOT_FOUND)
    if not user_model_is_on_the_clock(model, datetime.now(UTC)):
        raise api_error(
            503,
            f"User-provided {model.kind} model {model.id} is off the clock",
            ErrorType.MODEL_OFF_THE_CLOCK,
        )
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


async def _prime_stream(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Run admission before response headers are committed.

    Spend-window reservation normally happens at the top of the async
    generator. Pulling one item here makes its verdict available to response
    middleware and lets a 429 use the normal JSON error path instead of becoming
    an error event after an HTTP 200 has already started.
    """
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        return _empty_stream()

    async def primed() -> AsyncIterator[bytes]:
        try:
            yield first
            async for chunk in stream:
                yield chunk
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    return primed()


async def _empty_stream() -> AsyncIterator[bytes]:
    if False:  # pragma: no cover - establishes the async-iterator shape
        yield b""


async def _candidate_stream_bytes(
    body: dict[str, Any],
    candidates: list[Model],
    *,
    requested_model: str,
    principal: Principal,
    settings: Settings,
    app_name: str,
    usage_type: UsageType | None = None,
    request: Request | None = None,
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
        request=request,
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
