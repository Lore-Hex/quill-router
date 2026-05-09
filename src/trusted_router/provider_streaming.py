from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from trusted_router.provider_types import ProviderStreamState


def stream_openai_chunks(
    *,
    request_id: str,
    model_id: str,
    text: str,
    finish_reason: str,
) -> AsyncIterator[bytes]:
    async def iterator() -> AsyncIterator[bytes]:
        created = int(time.time())
        role = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
        yield f"data: {json_dumps(role)}\n\n".encode()
        for token in chunk_text(text):
            chunk = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
            }
            yield f"data: {json_dumps(chunk)}\n\n".encode()
        done = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        yield f"data: {json_dumps(done)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return iterator()


def openai_stream_chunk(
    *,
    request_id: str,
    model_id: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> bytes:
    return (
        "data: "
        + json_dumps(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
        )
        + "\n\n"
    ).encode()


def anthropic_sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json_dumps(payload)}\n\n".encode()


def chunk_text(text: str) -> list[str]:
    words = text.split(" ")
    if len(words) <= 1:
        return [text]
    return [word + (" " if i < len(words) - 1 else "") for i, word in enumerate(words)]


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def safe_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.reason_phrase or "provider error"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type") or error.get("code")
            if message:
                return str(message)[:240]
        if "message" in data:
            return str(data["message"])[:240]
    return resp.reason_phrase or "provider error"


async def safe_stream_error_message(resp: httpx.Response) -> str:
    body = await resp.aread()
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return resp.reason_phrase or "provider error"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type") or error.get("code")
            if message:
                return str(message)[:240]
        if "message" in data:
            return str(data["message"])[:240]
    return resp.reason_phrase or "provider error"


def record_openai_stream_payload(state: ProviderStreamState, payload: dict[str, Any]) -> None:
    if payload.get("id"):
        state.request_id = str(payload["id"])
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    state.record_text(content)
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                state.finish_reason = str(finish_reason)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        if usage.get("prompt_tokens") is not None:
            state.input_tokens = int(usage["prompt_tokens"])
        if usage.get("completion_tokens") is not None:
            state.output_tokens = int(usage["completion_tokens"])
        # OpenAI exposes cache hits via `prompt_tokens_details.cached_tokens`.
        # We also accept the legacy top-level `cached_tokens` field that
        # some compatible servers send.
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            state.cached_input_tokens = int(details["cached_tokens"])
        elif usage.get("cached_tokens") is not None:
            state.cached_input_tokens = int(usage["cached_tokens"])
        state.usage_estimated = False


def record_anthropic_stream_payload(state: ProviderStreamState, payload: dict[str, Any]) -> str | None:
    payload_type = payload.get("type")
    if payload_type == "message_start":
        message = payload.get("message")
        if isinstance(message, dict):
            if message.get("id"):
                state.request_id = str(message["id"])
            usage = message.get("usage")
            if isinstance(usage, dict):
                if usage.get("input_tokens") is not None:
                    state.input_tokens = int(usage["input_tokens"])
                if usage.get("output_tokens") is not None:
                    state.output_tokens = int(usage["output_tokens"])
                # Anthropic exposes prompt-cache hits via
                # `cache_read_input_tokens`. Cache writes
                # (`cache_creation_input_tokens`) bill at full + 25%
                # uplift but we don't separately track that yet.
                if usage.get("cache_read_input_tokens") is not None:
                    state.cached_input_tokens = int(usage["cache_read_input_tokens"])
                state.usage_estimated = False
    elif payload_type == "content_block_delta":
        delta = payload.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text = delta.get("text")
            if isinstance(text, str):
                state.record_text(text)
                return text
    elif payload_type == "message_delta":
        delta = payload.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason"):
            state.finish_reason = str(delta["stop_reason"])
        usage = payload.get("usage")
        if isinstance(usage, dict):
            if usage.get("input_tokens") is not None:
                state.input_tokens = int(usage["input_tokens"])
            if usage.get("output_tokens") is not None:
                state.output_tokens = int(usage["output_tokens"])
            state.usage_estimated = False
    elif payload_type == "error":
        error = payload.get("error")
        if isinstance(error, dict):
            state.finish_reason = str(error.get("type") or "error")
    return None


def record_gemini_stream_payload(state: ProviderStreamState, payload: dict[str, Any]) -> str | None:
    if payload.get("responseId"):
        state.request_id = str(payload["responseId"])
    text_parts: list[str] = []
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            text_parts.append(part["text"])
            finish_reason = candidate.get("finishReason")
            if finish_reason:
                state.finish_reason = gemini_finish_reason(str(finish_reason))
    usage = payload.get("usageMetadata")
    if isinstance(usage, dict):
        if usage.get("promptTokenCount") is not None:
            state.input_tokens = int(usage["promptTokenCount"])
        if usage.get("candidatesTokenCount") is not None:
            state.output_tokens = int(usage["candidatesTokenCount"])
        # Gemini exposes cache hits via `cachedContentTokenCount`.
        if usage.get("cachedContentTokenCount") is not None:
            state.cached_input_tokens = int(usage["cachedContentTokenCount"])
        state.usage_estimated = False
    text = "".join(text_parts)
    if text:
        state.record_text(text)
        return text
    return None


def gemini_finish_reason(reason: str) -> str:
    normalized = reason.upper()
    if normalized in {"STOP", "FINISH_REASON_UNSPECIFIED"}:
        return "stop"
    if normalized == "MAX_TOKENS":
        return "length"
    if normalized in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
        return "content_filter"
    return "stop"
