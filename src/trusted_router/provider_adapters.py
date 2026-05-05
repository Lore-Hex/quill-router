from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from trusted_router.catalog import PROVIDERS, Model
from trusted_router.provider_payloads import (
    anthropic_messages_payload,
    gemini_payload,
    messages,
    upstream_model_id,
)
from trusted_router.provider_streaming import (
    openai_stream_chunk,
    record_anthropic_stream_payload,
    record_gemini_stream_payload,
    record_openai_stream_payload,
    safe_error_message,
    safe_stream_error_message,
)
from trusted_router.provider_types import (
    ProviderError,
    ProviderResult,
    ProviderStreamState,
    estimate_tokens_from_messages,
    estimate_tokens_from_text,
)


async def openai_compatible_chat(
    model: Model,
    request: dict[str, Any],
    *,
    api_key: str,
    base_url: str,
) -> ProviderResult:
    started = time.monotonic()
    payload = {
        "model": upstream_model_id(model),
        "messages": messages(request),
        "stream": False,
        "temperature": request.get("temperature"),
        "max_tokens": request.get("max_tokens"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            json=payload,
        )
    if resp.status_code >= 400:
        raise ProviderError(model.provider, resp.status_code, safe_error_message(resp))
    data = resp.json()
    choice = data.get("choices", [{}])[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    text = str(message.get("content") or "")
    return ProviderResult(
        text=text,
        input_tokens=int(usage.get("prompt_tokens") or estimate_tokens_from_messages(messages(request))),
        output_tokens=int(usage.get("completion_tokens") or estimate_tokens_from_text(text)),
        finish_reason=str(choice.get("finish_reason") or "stop"),
        provider_name=PROVIDERS[model.provider].name,
        request_id=str(data.get("id") or f"req-{uuid.uuid4()}"),
        usage_estimated=not bool(usage),
        elapsed_seconds=max(time.monotonic() - started, 0.001),
    )


async def openai_compatible_chat_stream(
    model: Model,
    request: dict[str, Any],
    state: ProviderStreamState,
    *,
    api_key: str,
    base_url: str,
) -> AsyncIterator[bytes]:
    payload = {
        "model": upstream_model_id(model),
        "messages": messages(request),
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": request.get("temperature"),
        "max_tokens": request.get("max_tokens"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                raise ProviderError(model.provider, resp.status_code, await safe_stream_error_message(resp))
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    yield f"{line}\n\n".encode()
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    yield b"data: [DONE]\n\n"
                    break
                try:
                    payload_chunk = json.loads(data)
                except ValueError:
                    yield f"data: {data}\n\n".encode()
                    continue
                record_openai_stream_payload(state, payload_chunk)
                yield f"data: {data}\n\n".encode()


async def anthropic_chat(
    model: Model,
    request: dict[str, Any],
    *,
    api_key: str,
) -> ProviderResult:
    started = time.monotonic()
    payload = anthropic_messages_payload(model, request, stream=False)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json=payload,
        )
    if resp.status_code >= 400:
        raise ProviderError("anthropic", resp.status_code, safe_error_message(resp))
    data = resp.json()
    text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
    usage = data.get("usage") or {}
    return ProviderResult(
        text=text,
        input_tokens=int(usage.get("input_tokens") or estimate_tokens_from_messages(messages(request))),
        output_tokens=int(usage.get("output_tokens") or estimate_tokens_from_text(text)),
        finish_reason=str(data.get("stop_reason") or "stop"),
        provider_name="Anthropic",
        request_id=str(data.get("id") or f"req-{uuid.uuid4()}"),
        usage_estimated=not bool(usage),
        elapsed_seconds=max(time.monotonic() - started, 0.001),
    )


async def anthropic_messages_stream(
    model: Model,
    request: dict[str, Any],
    state: ProviderStreamState,
    *,
    api_key: str,
    output_format: str,
) -> AsyncIterator[bytes]:
    payload = anthropic_messages_payload(model, request, stream=True)
    created = int(time.time())
    openai_done_sent = False
    openai_role_sent = False
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                raise ProviderError("anthropic", resp.status_code, await safe_stream_error_message(resp))
            event_name = ""
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    if output_format == "anthropic":
                        yield f"{line}\n\n".encode()
                    continue
                data = line[5:].strip()
                try:
                    payload_chunk = json.loads(data)
                except ValueError:
                    if output_format == "anthropic":
                        if event_name:
                            yield f"event: {event_name}\n".encode()
                        yield f"data: {data}\n\n".encode()
                    continue

                text_delta = record_anthropic_stream_payload(state, payload_chunk)
                if output_format == "anthropic":
                    if event_name:
                        yield f"event: {event_name}\n".encode()
                    yield f"data: {data}\n\n".encode()
                    continue
                if not openai_role_sent and payload_chunk.get("type") in {
                    "message_start",
                    "content_block_start",
                    "content_block_delta",
                    "message_stop",
                }:
                    yield openai_stream_chunk(
                        request_id=state.request_id,
                        model_id=model.id,
                        created=created,
                        delta={"role": "assistant", "content": ""},
                        finish_reason=None,
                    )
                    openai_role_sent = True
                if text_delta:
                    yield openai_stream_chunk(
                        request_id=state.request_id,
                        model_id=model.id,
                        created=created,
                        delta={"content": text_delta},
                        finish_reason=None,
                    )
                if payload_chunk.get("type") == "message_stop":
                    yield openai_stream_chunk(
                        request_id=state.request_id,
                        model_id=model.id,
                        created=created,
                        delta={},
                        finish_reason=state.finish_reason,
                    )
                    yield b"data: [DONE]\n\n"
                    openai_done_sent = True

    if output_format == "openai" and not openai_done_sent:
        yield openai_stream_chunk(
            request_id=state.request_id,
            model_id=model.id,
            created=created,
            delta={},
            finish_reason=state.finish_reason,
        )
        yield b"data: [DONE]\n\n"


async def gemini_chat(model: Model, request: dict[str, Any], *, api_key: str) -> ProviderResult:
    started = time.monotonic()
    upstream_model = upstream_model_id(model)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{upstream_model}:generateContent",
            params={"key": api_key},
            json=gemini_payload(request),
        )
    if resp.status_code >= 400:
        raise ProviderError("gemini", resp.status_code, safe_error_message(resp))
    data = resp.json()
    candidate = (data.get("candidates") or [{}])[0]
    parts = ((candidate.get("content") or {}).get("parts") or [])
    text = _gemini_parts_to_openai_content(parts)
    usage = data.get("usageMetadata") or {}
    return ProviderResult(
        text=text,
        input_tokens=int(usage.get("promptTokenCount") or estimate_tokens_from_messages(messages(request))),
        output_tokens=int(usage.get("candidatesTokenCount") or estimate_tokens_from_text(text)),
        finish_reason=str(candidate.get("finishReason") or "stop").lower(),
        provider_name="Gemini",
        request_id=f"req-{uuid.uuid4()}",
        usage_estimated=not bool(usage),
        elapsed_seconds=max(time.monotonic() - started, 0.001),
    )


def _gemini_parts_to_openai_content(parts: list[Any]) -> str:
    """Flatten Gemini response `parts` into an OpenAI-shaped `content`
    string. Text parts join with newlines; image parts (Nano Banana,
    `gemini-3.1-flash-image-preview`, etc.) are emitted as
    `data:<mime>;base64,<body>` URLs so callers like forty.news that
    regex-scan `choices[0].message.content` for `data:image/...;base64,...`
    pick up the image without needing a separate `images` array.

    Without this, Gemini's `inline_data` parts were silently dropped and
    image-generation requests through TR returned an empty content
    string."""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
        # Gemini's REST API has historically returned both spellings.
        inline = part.get("inline_data") or part.get("inlineData")
        if isinstance(inline, dict):
            mime_type = inline.get("mime_type") or inline.get("mimeType") or "application/octet-stream"
            body = inline.get("data")
            if isinstance(body, str) and body:
                chunks.append(f"data:{mime_type};base64,{body}")
    return "\n".join(chunks)


async def gemini_chat_stream(
    model: Model,
    request: dict[str, Any],
    state: ProviderStreamState,
    *,
    api_key: str,
) -> AsyncIterator[bytes]:
    upstream_model = upstream_model_id(model)
    created = int(time.time())
    role_sent = False
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"https://generativelanguage.googleapis.com/v1beta/models/{upstream_model}:streamGenerateContent",
            params={"key": api_key, "alt": "sse"},
            json=gemini_payload(request),
        ) as resp:
            if resp.status_code >= 400:
                raise ProviderError("gemini", resp.status_code, await safe_stream_error_message(resp))
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload_chunk = json.loads(data)
                except ValueError:
                    continue
                text_delta = record_gemini_stream_payload(state, payload_chunk)
                if not role_sent:
                    yield openai_stream_chunk(
                        request_id=state.request_id,
                        model_id=model.id,
                        created=created,
                        delta={"role": "assistant", "content": ""},
                        finish_reason=None,
                    )
                    role_sent = True
                if text_delta:
                    yield openai_stream_chunk(
                        request_id=state.request_id,
                        model_id=model.id,
                        created=created,
                        delta={"content": text_delta},
                        finish_reason=None,
                    )
    yield openai_stream_chunk(
        request_id=state.request_id,
        model_id=model.id,
        created=created,
        delta={},
        finish_reason=state.finish_reason,
    )
    yield b"data: [DONE]\n\n"
