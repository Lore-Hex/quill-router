from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS
from trusted_router.providers import ProviderClient
from trusted_router.secrets import LocalKeyFile
from trusted_router.storage import STORE


@pytest.mark.asyncio
async def test_openai_stream_stops_at_first_done_and_ignores_late_bytes(tmp_path, monkeypatch) -> None:
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
            yield 'data: {"id":"dup_done","choices":[{"delta":{"content":"ok"},"finish_reason":null}]}'
            yield "data: [DONE]"
            yield 'data: {"choices":[{"delta":{"content":"late"},"finish_reason":null}]}'
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

        def stream(self, *_args: Any, **_kwargs: Any) -> FakeStreamResponse:
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    model = MODELS["openai/gpt-4o-mini"]
    state = client.new_stream_state(model, {"messages": [{"role": "user", "content": "hello"}]})

    body = b"".join(
        [
            chunk
            async for chunk in client.stream_chat(
                model,
                {"messages": [{"role": "user", "content": "hello"}]},
                state,
            )
        ]
    )

    assert body.count(b"data: [DONE]") == 1
    assert b"late" not in body
    assert state.to_result().text == "ok"


@pytest.mark.asyncio
async def test_openai_stream_preserves_partial_state_when_provider_disconnects(tmp_path, monkeypatch) -> None:
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
            yield 'data: {"id":"disconnect","choices":[{"delta":{"content":"partial"},"finish_reason":null}]}'
            raise httpx.ReadError("provider socket closed")

        async def aread(self) -> bytes:
            return b""

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def stream(self, *_args: Any, **_kwargs: Any) -> FakeStreamResponse:
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    model = MODELS["openai/gpt-4o-mini"]
    state = client.new_stream_state(model, {"messages": [{"role": "user", "content": "hello"}]})

    with pytest.raises(httpx.ReadError, match="provider socket closed"):
        _ = [
            chunk
            async for chunk in client.stream_chat(
                model,
                {"messages": [{"role": "user", "content": "hello"}]},
                state,
            )
        ]

    assert state.request_id == "disconnect"
    assert state.to_result().text == "partial"
    assert state.usage_estimated is True


@pytest.mark.asyncio
async def test_anthropic_late_error_event_finishes_openai_stream_with_error_reason(tmp_path, monkeypatch) -> None:
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
            yield "event: error"
            yield 'data: {"type":"error","error":{"type":"overloaded_error","message":"slow down"}}'

        async def aread(self) -> bytes:
            return b""

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def stream(self, *_args: Any, **_kwargs: Any) -> FakeStreamResponse:
            return FakeStreamResponse()

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    model = MODELS["anthropic/claude-3-5-sonnet"]
    state = client.new_stream_state(model, {"messages": [{"role": "user", "content": "hello"}]})

    body = b"".join(
        [
            chunk
            async for chunk in client.stream_chat(
                model,
                {"messages": [{"role": "user", "content": "hello"}]},
                state,
            )
        ]
    )

    assert body.endswith(b"data: [DONE]\n\n")
    assert b'"finish_reason":"overloaded_error"' in body
    assert state.finish_reason == "overloaded_error"


def test_route_stream_failure_refunds_reserved_quota(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    def broken_stream(self, model, body, state):
        async def iterator():
            state.request_id = "broken-stream"
            state.record_text("partial")
            yield b'data: {"id":"broken-stream","choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise RuntimeError("provider stream broke")

        return iterator()

    monkeypatch.setattr(ProviderClient, "stream_chat", broken_stream)
    key = next(iter(STORE.api_keys.keys.values()))
    account = STORE.credits[key.workspace_id]
    before_credits = account.total_credits_microdollars

    with pytest.raises(RuntimeError, match="provider stream broke"):
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=inference_headers,
            json={
                "model": "openai/gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        ) as response:
            _ = b"".join(response.iter_bytes())

    assert account.total_credits_microdollars == before_credits
    assert account.total_usage_microdollars == 0
    assert account.reserved_microdollars == 0
    assert key.reserved_microdollars == 0
    assert STORE.generation_store.generations == {}
