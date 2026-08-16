from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import HTTPException

from trusted_router.byok_crypto import decrypt_control_secret
from trusted_router.config import Settings
from trusted_router.errors import api_error, error_body
from trusted_router.services.safe_egress import assert_public_url
from trusted_router.services.user_model_secrets import (
    USER_MODEL_ENDPOINT_KEY_PURPOSE,
    USER_MODEL_SIGNING_PURPOSE,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import UserProvidedModel
from trusted_router.types import ErrorType
from trusted_router.user_model_rules import DispatchBudget, dispatch_budget, sign_request_body

_KEEPALIVE_SECONDS = 15.0


@dataclass(frozen=True)
class BufferedUserModelDispatch:
    body: dict[str, Any]
    first_token_seconds: float
    elapsed_seconds: float


class _MalformedOwnerResponse(ValueError):
    pass


async def dispatch_user_model(
    model: UserProvidedModel,
    body: dict[str, Any],
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> BufferedUserModelDispatch:
    """Dispatch a non-streaming caller to a user-operated OpenAI endpoint."""
    try:
        result = await _dispatch_user_model(
            model,
            body,
            settings,
            transport=transport,
        )
    except (TimeoutError, httpx.TimeoutException) as exc:
        STORE.record_user_model_dispatch_result(model.id, success=False)
        raise _timeout_error(model) from exc
    except _MalformedOwnerResponse as exc:
        STORE.record_user_model_dispatch_result(model.id, success=False)
        raise _malformed_error(model) from exc
    except httpx.HTTPError as exc:
        STORE.record_user_model_dispatch_result(model.id, success=False)
        raise _upstream_error(model) from exc
    except Exception:
        STORE.record_user_model_dispatch_result(model.id, success=False)
        raise
    STORE.record_user_model_dispatch_result(model.id, success=True)
    return result


async def stream_user_model(
    model: UserProvidedModel,
    body: dict[str, Any],
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[bytes]:
    """Dispatch a streaming caller and always finish failures as valid SSE."""
    succeeded = False
    try:
        async for chunk in _stream_user_model(
            model,
            body,
            settings,
            transport=transport,
        ):
            yield chunk
        succeeded = True
    except (TimeoutError, httpx.TimeoutException):
        yield _sse_error(_timeout_error(model))
        yield b"data: [DONE]\n\n"
    except HTTPException as exc:
        yield _sse_error(exc)
        yield b"data: [DONE]\n\n"
    except httpx.HTTPError:
        yield _sse_error(_upstream_error(model))
        yield b"data: [DONE]\n\n"
    except Exception:
        yield _sse_error(_malformed_error(model))
        yield b"data: [DONE]\n\n"
    finally:
        STORE.record_user_model_dispatch_result(model.id, success=succeeded)


async def _dispatch_user_model(
    model: UserProvidedModel,
    body: dict[str, Any],
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> BufferedUserModelDispatch:
    started_at = time.monotonic()
    budget = dispatch_budget(model.kind)
    request_url, request_body, headers = _owner_request(model, body, settings)
    timeout = httpx.Timeout(
        connect=float(budget.connect),
        read=None,
        write=float(budget.connect),
        pool=float(budget.connect),
    )
    async with asyncio.timeout(budget.total):
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
        ) as client:
            request = client.build_request(
                "POST",
                request_url,
                content=request_body,
                headers=headers,
            )
            response = await client.send(request, stream=True)
            try:
                _require_success(response, model)
                raw, first_token_seconds = await _read_owner_body(
                    response,
                    budget,
                    started_at=started_at,
                )
            finally:
                await response.aclose()

    if model.supports_streaming:
        result_body = _aggregate_owner_sse(raw, requested_model_id=model.id)
    else:
        result_body = _parse_buffered_completion(raw)
    return BufferedUserModelDispatch(
        body=result_body,
        first_token_seconds=max(first_token_seconds, 0.001),
        elapsed_seconds=max(time.monotonic() - started_at, 0.001),
    )


async def _stream_user_model(
    model: UserProvidedModel,
    body: dict[str, Any],
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> AsyncIterator[bytes]:
    started_at = time.monotonic()
    budget = dispatch_budget(model.kind)
    request_url, request_body, headers = _owner_request(model, body, settings)
    timeout = httpx.Timeout(
        connect=float(budget.connect),
        read=None,
        write=float(budget.connect),
        pool=float(budget.connect),
    )
    async with asyncio.timeout(budget.total):
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
        ) as client:
            request = client.build_request(
                "POST",
                request_url,
                content=request_body,
                headers=headers,
            )
            send_task = asyncio.create_task(client.send(request, stream=True))
            async for keepalive in _keepalive_until(
                send_task,
                deadline=started_at + budget.first_byte,
            ):
                yield keepalive
            response = send_task.result()
            try:
                _require_success(response, model)
                iterator = response.aiter_bytes()
                first_task: asyncio.Future[bytes] = asyncio.ensure_future(anext(iterator))
                async for keepalive in _keepalive_until(
                    first_task,
                    deadline=started_at + budget.first_byte,
                ):
                    yield keepalive
                try:
                    first = first_task.result()
                except StopAsyncIteration as exc:
                    raise _MalformedOwnerResponse("empty owner response") from exc

                if model.supports_streaming:
                    async for frame in _validated_owner_sse(
                        first,
                        iterator,
                        idle_timeout=float(budget.idle),
                    ):
                        yield frame
                else:
                    raw = await _read_remaining_owner_body(
                        first,
                        iterator,
                        idle_timeout=float(budget.idle),
                    )
                    completion = _parse_buffered_completion(raw)
                    for frame in _buffered_completion_frames(completion, model.id):
                        yield frame
            finally:
                await response.aclose()


def _owner_request(
    model: UserProvidedModel,
    body: dict[str, Any],
    settings: Settings,
) -> tuple[str, bytes, dict[str, str]]:
    request_url = f"{model.endpoint_url.rstrip('/')}/chat/completions"
    # Re-resolve at dispatch time. Registration/probe success is not authority
    # for a later DNS answer, and redirects stay disabled below.
    assert_public_url(
        request_url,
        allow_http=settings.environment in {"local", "test"},
    )
    signing_secret = _decrypt_signing_secret(model, settings)
    endpoint_api_key = _decrypt_endpoint_key(model, settings)
    owner_body = {
        **body,
        "model": model.upstream_model_id,
        "stream": model.supports_streaming,
    }
    request_body = json.dumps(owner_body, separators=(",", ":")).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "TR-Signature": sign_request_body(signing_secret, request_body, datetime.now(UTC)),
    }
    if endpoint_api_key is not None:
        headers["authorization"] = f"Bearer {endpoint_api_key}"
    return request_url, request_body, headers


async def _read_owner_body(
    response: httpx.Response,
    budget: DispatchBudget,
    *,
    started_at: float,
) -> tuple[bytes, float]:
    iterator = response.aiter_bytes()
    remaining = max(0.001, started_at + budget.first_byte - time.monotonic())
    try:
        first = await asyncio.wait_for(anext(iterator), timeout=remaining)
    except StopAsyncIteration as exc:
        raise _MalformedOwnerResponse("empty owner response") from exc
    first_token_seconds = time.monotonic() - started_at
    raw = await _read_remaining_owner_body(
        first,
        iterator,
        idle_timeout=float(budget.idle),
    )
    return raw, first_token_seconds


async def _read_remaining_owner_body(
    first: bytes,
    iterator: AsyncIterator[bytes],
    *,
    idle_timeout: float,
) -> bytes:
    chunks = [first]
    while True:
        try:
            chunk = await asyncio.wait_for(anext(iterator), timeout=idle_timeout)
        except StopAsyncIteration:
            break
        chunks.append(chunk)
    return b"".join(chunks)


async def _keepalive_until(
    task: asyncio.Future[Any],
    *,
    deadline: float,
) -> AsyncIterator[bytes]:
    try:
        while not task.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            done, _pending = await asyncio.wait(
                {task},
                timeout=min(_KEEPALIVE_SECONDS, remaining),
            )
            if not done:
                yield b": keepalive\n\n"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _validated_owner_sse(
    first: bytes,
    iterator: AsyncIterator[bytes],
    *,
    idle_timeout: float,
) -> AsyncIterator[bytes]:
    pending = first
    saw_chunk = False
    saw_done = False
    while True:
        events, pending = _take_sse_events(pending)
        for event in events:
            payload = _event_data(event)
            if payload is None:
                continue
            if payload == b"[DONE]":
                if not saw_chunk:
                    raise _MalformedOwnerResponse("owner stream had no chunks")
                saw_done = True
                yield b"data: [DONE]\n\n"
                break
            _parse_stream_chunk(payload)
            saw_chunk = True
            yield b"data: " + payload + b"\n\n"
        if saw_done:
            break
        try:
            pending += await asyncio.wait_for(anext(iterator), timeout=idle_timeout)
        except StopAsyncIteration:
            if pending.strip():
                payload = _event_data(pending)
                if payload == b"[DONE]":
                    if not saw_chunk:
                        raise _MalformedOwnerResponse("owner stream had no chunks") from None
                    saw_done = True
                    yield b"data: [DONE]\n\n"
                elif payload is not None:
                    _parse_stream_chunk(payload)
                    saw_chunk = True
                    yield b"data: " + payload + b"\n\n"
            break
    if not saw_chunk or not saw_done:
        raise _MalformedOwnerResponse("owner stream was incomplete")


def _take_sse_events(raw: bytes) -> tuple[list[bytes], bytes]:
    normalized = raw.replace(b"\r\n", b"\n")
    parts = normalized.split(b"\n\n")
    return parts[:-1], parts[-1]


def _event_data(event: bytes) -> bytes | None:
    values = [line[5:].strip() for line in event.splitlines() if line.startswith(b"data:")]
    if not values:
        return None
    return b"\n".join(values)


def _parse_stream_chunk(payload: bytes) -> dict[str, Any]:
    try:
        chunk = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _MalformedOwnerResponse("invalid owner SSE JSON") from exc
    choices = chunk.get("choices") if isinstance(chunk, dict) else None
    if (
        not isinstance(chunk, dict)
        or chunk.get("object") != "chat.completion.chunk"
        or not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
        or not isinstance(choices[0].get("delta"), dict)
    ):
        raise _MalformedOwnerResponse("invalid owner SSE chunk")
    return chunk


def _aggregate_owner_sse(raw: bytes, *, requested_model_id: str) -> dict[str, Any]:
    normalized = raw.replace(b"\r\n", b"\n")
    content: list[str] = []
    role = "assistant"
    finish_reason: str | None = None
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    saw_chunk = False
    saw_done = False
    usage: dict[str, Any] | None = None
    for event in normalized.split(b"\n\n"):
        payload = _event_data(event)
        if payload is None:
            continue
        if payload == b"[DONE]":
            saw_done = True
            continue
        chunk = _parse_stream_chunk(payload)
        saw_chunk = True
        request_id = str(chunk.get("id") or request_id)
        created = int(chunk.get("created") or created)
        choice = chunk["choices"][0]
        delta = choice["delta"]
        if isinstance(delta.get("role"), str):
            role = delta["role"]
        if isinstance(delta.get("content"), str):
            content.append(delta["content"])
        if choice.get("finish_reason") is not None:
            finish_reason = str(choice["finish_reason"])
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
    if not saw_chunk or not saw_done:
        raise _MalformedOwnerResponse("owner stream was incomplete")
    result: dict[str, Any] = {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": requested_model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": role, "content": "".join(content)},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        result["usage"] = usage
    return result


def _parse_buffered_completion(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _MalformedOwnerResponse("invalid owner JSON") from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or body.get("object") != "chat.completion"
        or not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
        or not isinstance(choices[0].get("message"), dict)
    ):
        raise _MalformedOwnerResponse("invalid owner chat completion")
    return body


def _buffered_completion_frames(
    completion: dict[str, Any],
    requested_model_id: str,
) -> list[bytes]:
    choice = completion["choices"][0]
    message = choice["message"]
    request_id = str(completion.get("id") or f"chatcmpl-{uuid.uuid4().hex}")
    created = int(completion.get("created") or time.time())
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise _MalformedOwnerResponse("invalid owner message content")
    first = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": requested_model_id,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": str(message.get("role") or "assistant"),
                    "content": content or "",
                },
                "finish_reason": None,
            }
        ],
    }
    finish = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": requested_model_id,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": choice.get("finish_reason"),
            }
        ],
    }
    return [
        _sse_json(first),
        _sse_json(finish),
        b"data: [DONE]\n\n",
    ]


def _require_success(response: httpx.Response, model: UserProvidedModel) -> None:
    if 200 <= response.status_code < 300:
        return
    raise api_error(
        502,
        f"User-provided model {model.id} returned an upstream error",
        ErrorType.PROVIDER_ERROR,
    )


def _timeout_error(model: UserProvidedModel) -> HTTPException:
    return api_error(
        504,
        f"User-provided {model.kind} model {model.id} exceeded its dispatch budget",
        ErrorType.USER_MODEL_TIMEOUT,
    )


def _malformed_error(model: UserProvidedModel) -> HTTPException:
    return api_error(
        502,
        f"User-provided model {model.id} returned a malformed response",
        ErrorType.PROVIDER_ERROR,
    )


def _upstream_error(model: UserProvidedModel) -> HTTPException:
    return api_error(
        502,
        f"User-provided model {model.id} could not be reached",
        ErrorType.PROVIDER_ERROR,
    )


def _sse_error(exc: HTTPException) -> bytes:
    detail: Any = exc.detail
    if not isinstance(detail, dict):
        detail = error_body(exc.status_code, str(detail), ErrorType.PROVIDER_ERROR)
    return b"event: error\ndata: " + json.dumps(detail, separators=(",", ":")).encode() + b"\n\n"


def _sse_json(body: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"


def _decrypt_signing_secret(model: UserProvidedModel, settings: Settings) -> str:
    if model.encrypted_signing_secret is None:
        raise ValueError("missing user-model signing secret")
    return decrypt_control_secret(
        model.encrypted_signing_secret,
        settings,
        workspace_id=model.owner_workspace_id,
        purpose=USER_MODEL_SIGNING_PURPOSE,
    )


def _decrypt_endpoint_key(
    model: UserProvidedModel,
    settings: Settings,
) -> str | None:
    if model.encrypted_endpoint_api_key is None:
        return None
    return decrypt_control_secret(
        model.encrypted_endpoint_api_key,
        settings,
        workspace_id=model.owner_workspace_id,
        purpose=USER_MODEL_ENDPOINT_KEY_PURPOSE,
    )
