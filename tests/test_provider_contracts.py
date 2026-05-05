from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from trusted_router.catalog import MODELS
from trusted_router.providers import (
    ProviderClient,
    ProviderError,
    estimate_tokens_from_messages,
    estimate_tokens_from_text,
    stream_openai_chunks,
)
from trusted_router.secrets import LocalKeyFile


def test_token_estimators_have_minimums_and_handle_content_parts() -> None:
    assert estimate_tokens_from_text("") == 1
    assert estimate_tokens_from_text("abcd") == 1
    assert estimate_tokens_from_text("abcdefgh") == 2
    assert estimate_tokens_from_messages([]) == 1
    assert estimate_tokens_from_messages([{"content": [{"type": "text", "text": "hello"}]}]) >= 1


@pytest.mark.asyncio
async def test_stream_openai_chunks_are_valid_sse_json_and_reconstruct_text() -> None:
    chunks = [
        item async for item in stream_openai_chunks(
            request_id="req_1",
            model_id="openai/gpt-4o-mini",
            text="hello trusted router",
            finish_reason="stop",
        )
    ]
    lines = [chunk.decode().strip() for chunk in chunks]
    assert lines[0].startswith("data: ")
    assert lines[-1] == "data: [DONE]"

    payloads = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    reconstructed = "".join(
        payload["choices"][0]["delta"].get("content", "")
        for payload in payloads
    )
    assert reconstructed == "hello trusted router"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_openai_compatible_live_adapter_uses_provider_usage_and_headers(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("OPENAI_API_KEY=openai-value\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **_: Any):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": self.timeout})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_test",
                    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 2},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    result = await client.chat(
        MODELS["openai/gpt-4o-mini"],
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 5},
    )

    assert result.request_id == "chatcmpl_test"
    assert result.input_tokens == 7
    assert result.output_tokens == 2
    assert result.usage_estimated is False
    assert calls == [
        {
            "url": "https://api.openai.com/v1/chat/completions",
            "headers": {"authorization": "Bearer openai-value"},
            "json": {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "max_tokens": 5,
            },
            "timeout": 120,
        }
    ]


@pytest.mark.parametrize(
    ("provider_model", "env_key", "env_value", "expected_url", "expected_model"),
    [
        (
            "openai/gpt-4o-mini",
            "OPENAI_API_KEY",
            "openai-value",
            "https://api.openai.com/v1/chat/completions",
            "gpt-4o-mini",
        ),
        (
            "meta-llama/llama-3.1-8b-instruct",
            "CEREBRAS_API_KEY",
            "cerebras-value",
            "https://api.cerebras.ai/v1/chat/completions",
            "llama-3.1-8b-instruct",
        ),
        (
            "deepseek/deepseek-v4-flash",
            "DEEPSEEK_API_KEY",
            "deepseek-value",
            "https://api.deepseek.com/chat/completions",
            "deepseek-v4-flash",
        ),
        (
            "mistralai/mistral-small-2603",
            "MISTRAL_API_KEY",
            "mistral-value",
            "https://api.mistral.ai/v1/chat/completions",
            "mistral-small-2603",
        ),
        (
            "moonshotai/kimi-k2.6",
            "MOONSHOT_API_KEY",
            "kimi-value",
            "https://api.moonshot.ai/v1/chat/completions",
            "kimi-k2.6",
        ),
    ],
)
@pytest.mark.asyncio
async def test_new_openai_compatible_provider_platforms_use_native_keys_and_urls(
    provider_model: str,
    env_key: str,
    env_value: str,
    expected_url: str,
    expected_model: str,
    tmp_path,
    monkeypatch,
) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text(f"{env_key}={env_value}\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **_: Any):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": self.timeout})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_provider",
                    "choices": [{"message": {"content": "provider hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    result = await client.chat(
        MODELS[provider_model],
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 5},
    )

    assert result.text == "provider hello"
    assert result.usage_estimated is False
    assert calls == [
        {
            "url": expected_url,
            "headers": {"authorization": f"Bearer {env_value}"},
            "json": {
                "model": expected_model,
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "max_tokens": 5,
            },
            "timeout": 120,
        }
    ]


@pytest.mark.asyncio
async def test_kimi_prefers_kimi_api_key_but_accepts_moonshot_alias(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text(
        "MOONSHOT_API_KEY=moonshot-value\nKIMI_API_KEY=kimi-value\n",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **_: Any):
            calls.append({"url": url, "headers": headers, "json": json})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_kimi",
                    "choices": [{"message": {"content": "kimi hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    result = await client.chat(
        MODELS["moonshotai/kimi-k2.6"],
        {"messages": [{"role": "user", "content": "hello"}]},
    )

    assert result.text == "kimi hello"
    assert calls[0]["headers"] == {"authorization": "Bearer kimi-value"}


@pytest.mark.skip(
    reason="Vertex provider is dormant — TR's GCP project doesn't yet have "
    "Anthropic-on-Vertex / Gemini-on-Vertex quota approved, so no models "
    "currently route through the Vertex adapter. Re-enable this test when "
    "vertex models are added back to the catalog."
)
@pytest.mark.asyncio
async def test_vertex_platform_uses_gcp_identity_not_provider_api_key(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **_: Any):
            calls.append({"url": url, "headers": headers, "json": json})
            return httpx.Response(
                200,
                json={
                    "id": "vertex_chat",
                    "choices": [{"message": {"content": "vertex hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 3},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        ProviderClient,
        "_vertex_auth_and_base_url",
        lambda _self: (
            "ya29.vertex-token",
            "https://europe-west4-aiplatform.googleapis.com/v1beta1/projects/quill/locations/europe-west4/endpoints/openapi",
        ),
    )
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    result = await client.chat(
        MODELS["vertex/gemini-2.5-flash"],
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 5},
    )

    assert result.text == "vertex hello"
    assert calls == [
        {
            "url": (
                "https://europe-west4-aiplatform.googleapis.com/v1beta1/projects/quill/"
                "locations/europe-west4/endpoints/openapi/chat/completions"
            ),
            "headers": {"authorization": "Bearer ya29.vertex-token"},
            "json": {
                "model": "google/gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "max_tokens": 5,
            },
        }
    ]


@pytest.mark.asyncio
async def test_openai_compatible_stream_adapter_passes_through_sse_and_usage(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("OPENAI_API_KEY=openai-value\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeStreamResponse:
        status_code = 200
        reason_phrase = "OK"

        async def __aenter__(self) -> FakeStreamResponse:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def aiter_lines(self) -> AsyncIterator[str]:
            yield 'data: {"id":"chatcmpl_stream","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}'
            yield 'data: {"id":"chatcmpl_stream","choices":[{"delta":{"content":"hel"},"finish_reason":null}]}'
            yield (
                'data: {"id":"chatcmpl_stream","choices":[{"delta":{"content":"lo"},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":2}}'
            )
            yield "data: [DONE]"

        async def aread(self) -> bytes:
            return b""

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("streaming must not call the non-streaming post adapter")

        def stream(self, method: str, url: str, *, headers: dict[str, str], json: dict[str, Any], **_: Any):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": self.timeout,
                }
            )
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    request = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 5}
    model = MODELS["openai/gpt-4o-mini"]
    state = client.new_stream_state(model, request)

    chunks = [chunk async for chunk in client.stream_chat(model, request, state)]

    assert b"".join(chunks).endswith(b"data: [DONE]\n\n")
    assert b'"content":"hel"' in b"".join(chunks)
    result = state.to_result()
    assert result.text == "hello"
    assert result.request_id == "chatcmpl_stream"
    assert result.input_tokens == 11
    assert result.output_tokens == 2
    assert result.finish_reason == "stop"
    assert result.usage_estimated is False
    assert calls == [
        {
            "method": "POST",
            "url": "https://api.openai.com/v1/chat/completions",
            "headers": {"authorization": "Bearer openai-value"},
            "json": {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": 5,
            },
            "timeout": 120,
        }
    ]


@pytest.mark.asyncio
async def test_openai_compatible_stream_tolerates_malformed_sse_and_estimates_missing_usage(
    tmp_path,
    monkeypatch,
) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("OPENAI_API_KEY=openai-value\n", encoding="utf-8")

    class FakeStreamResponse:
        status_code = 200
        reason_phrase = "OK"

        async def __aenter__(self) -> FakeStreamResponse:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def aiter_lines(self) -> AsyncIterator[str]:
            yield ": keepalive"
            yield "data: {not-json"
            yield 'data: {"id":"chatcmpl_partial","choices":[{"delta":{"content":"hel"},"finish_reason":null}]}'
            yield 'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

        async def aread(self) -> bytes:
            return b""

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("streaming must not call the non-streaming post adapter")

        def stream(self, *_args: Any, **_kwargs: Any):
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    request = {"messages": [{"role": "user", "content": "hello"}]}
    model = MODELS["openai/gpt-4o-mini"]
    state = client.new_stream_state(model, request)

    chunks = [chunk async for chunk in client.stream_chat(model, request, state)]

    body = b"".join(chunks)
    assert b": keepalive" in body
    assert b"data: {not-json" in body
    assert b"data: [DONE]" in body
    result = state.to_result()
    assert result.request_id == "chatcmpl_partial"
    assert result.text == "hello"
    assert result.output_tokens == estimate_tokens_from_text("hello")
    assert result.usage_estimated is True


@pytest.mark.asyncio
async def test_openai_compatible_live_adapter_maps_provider_errors(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("CEREBRAS_API_KEY=cerebras-value\n", encoding="utf-8")

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any):
            return httpx.Response(429, json={"error": {"message": "slow down"}})

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    with pytest.raises(ProviderError) as exc_info:
        await client.chat(
            MODELS["meta-llama/llama-3.1-8b-instruct"],
            {"messages": [{"role": "user", "content": "hello"}]},
        )

    assert exc_info.value.provider == "cerebras"
    assert exc_info.value.status_code == 429
    assert exc_info.value.message == "slow down"


@pytest.mark.asyncio
async def test_anthropic_live_adapter_splits_system_prompt_and_uses_native_usage(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("ANTHROPIC_API_KEY=anthropic-value\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **_: Any):
            calls.append({"url": url, "headers": headers, "json": json})
            return httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "content": [{"type": "text", "text": "anthropic hello"}],
                    "usage": {"input_tokens": 8, "output_tokens": 3},
                    "stop_reason": "end_turn",
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    result = await client.chat(
        MODELS["anthropic/claude-sonnet-4.6"],
        {
            "max_tokens": 6,
            "messages": [
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        },
    )

    assert result.request_id == "msg_test"
    assert result.input_tokens == 8
    assert result.output_tokens == 3
    assert result.finish_reason == "end_turn"
    assert result.usage_estimated is False
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["headers"]["x-api-key"] == "anthropic-value"
    assert calls[0]["json"]["system"] == "system rules"
    assert calls[0]["json"]["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


@pytest.mark.asyncio
async def test_anthropic_chat_stream_uses_native_stream_and_emits_openai_chunks(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("ANTHROPIC_API_KEY=anthropic-value\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeStreamResponse:
        status_code = 200
        reason_phrase = "OK"

        async def __aenter__(self) -> FakeStreamResponse:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def aiter_lines(self) -> AsyncIterator[str]:
            yield "event: message_start"
            yield (
                'data: {"type":"message_start","message":{"id":"msg_stream","type":"message",'
                '"role":"assistant","usage":{"input_tokens":8,"output_tokens":1}}}'
            )
            yield "event: content_block_delta"
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hel"}}'
            yield "event: content_block_delta"
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}'
            yield "event: message_delta"
            yield (
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
                '"stop_sequence":null},"usage":{"output_tokens":2}}'
            )
            yield "event: message_stop"
            yield 'data: {"type":"message_stop"}'

        async def aread(self) -> bytes:
            return b""

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("streaming must not call the non-streaming Anthropic adapter")

        def stream(self, method: str, url: str, *, headers: dict[str, str], json: dict[str, Any], **_: Any):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": self.timeout,
                }
            )
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    request = {
        "messages": [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "hello"},
        ],
        "max_tokens": 5,
    }
    model = MODELS["anthropic/claude-sonnet-4.6"]
    state = client.new_stream_state(model, request)

    chunks = [chunk async for chunk in client.stream_chat(model, request, state)]
    body = b"".join(chunks)

    assert body.endswith(b"data: [DONE]\n\n")
    assert b'"id":"msg_stream"' in body
    assert b'"content":"hel"' in body
    assert b'"content":"lo"' in body
    result = state.to_result()
    assert result.text == "hello"
    assert result.request_id == "msg_stream"
    assert result.input_tokens == 8
    assert result.output_tokens == 2
    assert result.finish_reason == "end_turn"
    assert result.usage_estimated is False
    assert calls == [
        {
            "method": "POST",
            "url": "https://api.anthropic.com/v1/messages",
            "headers": {"x-api-key": "anthropic-value", "anthropic-version": "2023-06-01"},
            "json": {
                "model": "claude-sonnet-4.6",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 5,
                "stream": True,
                "system": "system rules",
            },
            "timeout": 120,
        }
    ]


@pytest.mark.asyncio
async def test_anthropic_messages_stream_passes_native_sse_and_records_usage(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("ANTHROPIC_API_KEY=anthropic-value\n", encoding="utf-8")

    class FakeStreamResponse:
        status_code = 200
        reason_phrase = "OK"

        async def __aenter__(self) -> FakeStreamResponse:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def aiter_lines(self) -> AsyncIterator[str]:
            yield "event: message_start"
            yield (
                'data: {"type":"message_start","message":{"id":"msg_native","type":"message",'
                '"role":"assistant","usage":{"input_tokens":9,"output_tokens":1}}}'
            )
            yield "event: content_block_delta"
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"native"}}'
            yield "event: message_delta"
            yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}'
            yield "event: message_stop"
            yield 'data: {"type":"message_stop"}'

        async def aread(self) -> bytes:
            return b""

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def stream(self, *_args: Any, **_kwargs: Any):
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    model = MODELS["anthropic/claude-sonnet-4.6"]
    state = client.new_stream_state(model, {"messages": [{"role": "user", "content": "hello"}]})

    chunks = [
        chunk async for chunk in client.stream_messages(model, {"messages": [{"role": "user", "content": "hello"}]}, state)
    ]
    body = b"".join(chunks)

    assert b"event: message_start\n" in body
    assert b"event: content_block_delta\n" in body
    assert b"data: [DONE]" not in body
    result = state.to_result()
    assert result.request_id == "msg_native"
    assert result.text == "native"
    assert result.input_tokens == 9
    assert result.output_tokens == 3
    assert result.finish_reason == "end_turn"
    assert result.usage_estimated is False


@pytest.mark.asyncio
async def test_gemini_live_adapter_maps_roles_and_usage(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("GEMINI_API_KEY=gemini-value\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            params: dict[str, str],
            json: dict[str, Any],
            **_: Any,
        ):
            calls.append({"url": url, "params": params, "json": json})
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "gemini hello"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    result = await client.chat(
        MODELS["google/gemini-2.5-flash"],
        {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "assistant", "content": "prior"},
                {"role": "user", "content": "hello"},
            ]
        },
    )

    assert result.text == "gemini hello"
    assert result.input_tokens == 9
    assert result.output_tokens == 4
    assert result.finish_reason == "stop"
    assert result.usage_estimated is False
    assert calls[0]["params"] == {"key": "gemini-value"}
    assert calls[0]["json"]["contents"] == [
        {"role": "user", "parts": [{"text": "system"}]},
        {"role": "model", "parts": [{"text": "prior"}]},
        {"role": "user", "parts": [{"text": "hello"}]},
    ]


@pytest.mark.asyncio
async def test_gemini_stream_adapter_uses_native_sse_and_records_usage(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("GEMINI_API_KEY=gemini-value\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class FakeStreamResponse:
        status_code = 200
        reason_phrase = "OK"

        async def __aenter__(self) -> FakeStreamResponse:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def aiter_lines(self) -> AsyncIterator[str]:
            yield (
                'data: {"responseId":"gemini_stream","candidates":[{"content":{"parts":'
                '[{"text":"hel"}],"role":"model"}}],"usageMetadata":{"promptTokenCount":9}}'
            )
            yield (
                'data: {"responseId":"gemini_stream","candidates":[{"content":{"parts":'
                '[{"text":"lo"}],"role":"model"},"finishReason":"STOP"}],'
                '"usageMetadata":{"promptTokenCount":9,"candidatesTokenCount":2}}'
            )

        async def aread(self) -> bytes:
            return b""

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("streaming must not call Gemini generateContent")

        def stream(
            self,
            method: str,
            url: str,
            *,
            params: dict[str, str],
            json: dict[str, Any],
            **_: Any,
        ):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "json": json,
                    "timeout": self.timeout,
                }
            )
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    model = MODELS["google/gemini-2.5-flash"]
    request = {
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 7,
        "temperature": 0.2,
    }
    state = client.new_stream_state(model, request)

    chunks = [chunk async for chunk in client.stream_chat(model, request, state)]
    body = b"".join(chunks)

    assert body.endswith(b"data: [DONE]\n\n")
    assert b'"id":"gemini_stream"' in body
    assert b'"content":"hel"' in body
    assert b'"content":"lo"' in body
    result = state.to_result()
    assert result.text == "hello"
    assert result.request_id == "gemini_stream"
    assert result.input_tokens == 9
    assert result.output_tokens == 2
    assert result.finish_reason == "stop"
    assert result.usage_estimated is False
    assert calls == [
        {
            "method": "POST",
            "url": (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.5-flash:streamGenerateContent"
            ),
            "params": {"key": "gemini-value", "alt": "sse"},
            "json": {
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
                "generationConfig": {"maxOutputTokens": 7, "temperature": 0.2},
            },
            "timeout": 120,
        }
    ]
