"""Inference service: chat/stream/messages runners.

The runners delegate financial bookkeeping to `inference_quota` (which owns
QuotaTicket / reserved_quota / apply_authorization_outcome) and HTTP-error
mapping to `inference_errors` (provider_http_error, default_provider_secret_ref,
rollover helpers). This file stays focused on transport: pulling responses
from the provider client, recording the generation row, and walking the
fallback candidate list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import HTTPException

from trusted_router.auth import Principal
from trusted_router.catalog import MODELS, PROVIDERS, Model, auto_candidate_models
from trusted_router.config import Settings
from trusted_router.providers import (
    ProviderClient,
    estimate_tokens_from_messages,
)
from trusted_router.routes.helpers import cost_microdollars, integer_body_field
from trusted_router.secrets import LocalKeyFile
from trusted_router.services.inference_errors import (
    all_candidates_failed,
    default_provider_secret_ref,
    http_error_message,
    is_rollover_http_error,
    provider_http_error,
)
from trusted_router.services.inference_quota import (
    QuotaTicket,
    apply_authorization_outcome,
    reserved_quota,
)
from trusted_router.storage import STORE, Generation


def provider_client(settings: Settings) -> ProviderClient:
    return ProviderClient(LocalKeyFile(settings.local_keys_file), live=settings.enable_live_providers)


async def run_chat_stream(
    body: dict[str, Any],
    model: Model,
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
) -> AsyncIterator[bytes]:
    async for chunk in _run_stream(
        body,
        model,
        principal,
        settings,
        app_name=app_name,
        stream_provider=lambda client, stream_model, stream_body, state: client.stream_chat(
            stream_model, stream_body, state
        ),
    ):
        yield chunk


async def run_chat_auto_stream(
    body: dict[str, Any],
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
) -> AsyncIterator[tuple[Model, bytes]]:
    async for item in run_chat_candidates_stream(
        body,
        auto_candidate_models(settings.auto_model_order),
        principal,
        settings,
        app_name=app_name,
    ):
        yield item


async def run_chat_candidates_stream(
    body: dict[str, Any],
    candidates: list[Model],
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
) -> AsyncIterator[tuple[Model, bytes]]:
    errors: list[str] = []
    last_error: HTTPException | None = None
    for index, model in enumerate(candidates):
        candidate_body = {**body, "model": model.id}
        stream = run_chat_stream(candidate_body, model, principal, settings, app_name=app_name)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            return
        except HTTPException as exc:
            last_error = exc
            if is_rollover_http_error(exc) and index < len(candidates) - 1:
                errors.append(http_error_message(exc))
                continue
            raise
        yield model, first
        async for chunk in stream:
            yield model, chunk
        return
    if last_error is not None and not errors:
        raise last_error
    raise all_candidates_failed(errors)


async def run_messages_stream(
    body: dict[str, Any],
    model: Model,
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
) -> AsyncIterator[bytes]:
    async for chunk in _run_stream(
        body,
        model,
        principal,
        settings,
        app_name=app_name,
        stream_provider=lambda client, stream_model, stream_body, state: client.stream_messages(
            stream_model, stream_body, state
        ),
    ):
        yield chunk


async def _run_stream(
    body: dict[str, Any],
    model: Model,
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
    stream_provider: Callable[
        [ProviderClient, Model, dict[str, Any], Any],
        AsyncIterator[bytes],
    ],
) -> AsyncIterator[bytes]:
    assert principal.api_key is not None
    client = provider_client(settings)
    input_estimate = estimate_tokens_from_messages(body.get("messages", []))
    reserve_amount = _estimate_reserve(body, model, input_estimate=input_estimate)
    async with reserved_quota(
        principal,
        model,
        reserve_amount=reserve_amount,
        input_tokens=input_estimate,
        streamed=True,
        region=settings.primary_region,
    ) as ticket:
        state = client.new_stream_state(model, body)
        async for chunk in stream_provider(client, model, body, state):
            yield chunk
        result = state.to_result()
        actual_cost = cost_microdollars(model, result.input_tokens, result.output_tokens)
        ticket.settle(actual_cost)
        STORE.add_generation(
            Generation.from_chat_result(
                result=result,
                workspace_id=principal.workspace.id,
                key_hash=principal.api_key.hash,
                model_id=model.id,
                app_name=app_name,
                actual_cost_microdollars=actual_cost,
                usage_type=ticket.usage_type,
                streamed=True,
                provider=model.provider,
                region=settings.primary_region,
            )
        )


async def run_chat(
    body: dict[str, Any],
    model: Model,
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
) -> tuple[Any, Generation]:
    assert principal.api_key is not None
    client = provider_client(settings)
    input_estimate = estimate_tokens_from_messages(body.get("messages", []))
    reserve_amount = _estimate_reserve(body, model, input_estimate=input_estimate)
    async with reserved_quota(
        principal,
        model,
        reserve_amount=reserve_amount,
        input_tokens=input_estimate,
        streamed=bool(body.get("stream")),
        region=settings.primary_region,
    ) as ticket:
        result = await client.chat(model, body)
        actual_cost = cost_microdollars(model, result.input_tokens, result.output_tokens)
        ticket.settle(actual_cost)
        generation = Generation.from_chat_result(
            result=result,
            workspace_id=principal.workspace.id,
            key_hash=principal.api_key.hash,
            model_id=model.id,
            app_name=app_name,
            actual_cost_microdollars=actual_cost,
            usage_type=ticket.usage_type,
            streamed=bool(body.get("stream")),
            provider=model.provider,
            region=settings.primary_region,
        )
        STORE.add_generation(generation)
        return result, generation


async def run_chat_auto(
    body: dict[str, Any],
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
) -> tuple[Any, Generation, Model, list[str]]:
    return await run_chat_candidates(
        body,
        auto_candidate_models(settings.auto_model_order),
        principal,
        settings,
        app_name=app_name,
    )


async def run_chat_candidates(
    body: dict[str, Any],
    candidates: list[Model],
    principal: Principal,
    settings: Settings,
    *,
    app_name: str,
) -> tuple[Any, Generation, Model, list[str]]:
    errors: list[str] = []
    last_error: HTTPException | None = None
    for index, model in enumerate(candidates):
        candidate_body = {**body, "model": model.id}
        try:
            result, generation = await run_chat(
                candidate_body,
                model,
                principal,
                settings,
                app_name=app_name,
            )
            return result, generation, model, errors
        except HTTPException as exc:
            last_error = exc
            if is_rollover_http_error(exc) and index < len(candidates) - 1:
                errors.append(http_error_message(exc))
                continue
            raise
    if last_error is not None and not errors:
        raise last_error
    raise all_candidates_failed(errors)


def _estimate_reserve(body: dict[str, Any], model: Model, *, input_estimate: int | None = None) -> int:
    input_estimate = input_estimate or estimate_tokens_from_messages(body.get("messages", []))
    max_tokens = integer_body_field(body, "max_tokens", default=512, minimum=1)
    return cost_microdollars(model, input_estimate, max_tokens)


# Re-exports used by routes/internal.py to avoid duplicating provider→name lookup.
__all__ = [
    "MODELS",
    "PROVIDERS",
    "QuotaTicket",
    "apply_authorization_outcome",
    "default_provider_secret_ref",
    "provider_client",
    "provider_http_error",
    "reserved_quota",
    "run_chat",
    "run_chat_auto",
    "run_chat_auto_stream",
    "run_chat_candidates",
    "run_chat_candidates_stream",
    "run_chat_stream",
    "run_messages_stream",
]
