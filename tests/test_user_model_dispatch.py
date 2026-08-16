from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from trusted_router.byok_crypto import encrypt_control_secret
from trusted_router.config import Settings
from trusted_router.services.user_model_dispatch import (
    dispatch_user_model,
    stream_user_model,
)
from trusted_router.services.user_model_secrets import (
    USER_MODEL_ENDPOINT_KEY_PURPOSE,
    USER_MODEL_SIGNING_PURPOSE,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import UserProvidedModel
from trusted_router.user_model_rules import sign_request_body

_SIGNING_FIXTURE = "dispatch-signing-fixture"
_ENDPOINT_KEY = "dispatch-endpoint-fixture"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )


class _DelayedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, first_delay: float) -> None:
        self._chunks = chunks
        self._first_delay = first_delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(self._first_delay)
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _model(
    settings: Settings,
    *,
    supports_streaming: bool,
    slug: str = "dispatch-test",
) -> UserProvidedModel:
    user = STORE.ensure_user("dispatch-owner@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    model = STORE.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Dispatch test",
        kind="machine",
        display_name="dispatch-owner",
        endpoint_url="https://owner.example/v1",
        upstream_model_id="owner-upstream",
        encrypted_endpoint_api_key=encrypt_control_secret(
            _ENDPOINT_KEY,
            settings,
            workspace_id=workspace.id,
            purpose=USER_MODEL_ENDPOINT_KEY_PURPOSE,
        ),
        encrypted_signing_secret=encrypt_control_secret(
            _SIGNING_FIXTURE,
            settings,
            workspace_id=workspace.id,
            purpose=USER_MODEL_SIGNING_PURPOSE,
        ),
        supports_streaming=supports_streaming,
        slug=slug,
    )
    return STORE.set_user_model_online(model.id, owner_user_id=user.id, online=True)


def _buffered_body(text: str = "buffered reply") -> dict[str, Any]:
    return {
        "id": "chatcmpl-owner-buffered",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "owner-upstream",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }


def _sse_body() -> bytes:
    chunks = [
        {
            "id": "chatcmpl-owner-stream",
            "object": "chat.completion.chunk",
            "created": 1_700_000_000,
            "model": "owner-upstream",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "owner "},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-owner-stream",
            "object": "chat.completion.chunk",
            "created": 1_700_000_000,
            "model": "owner-upstream",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "stream"},
                    "finish_reason": "stop",
                }
            ],
        },
    ]
    return b"".join(
        b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


def _request_body(*, stream: bool) -> dict[str, Any]:
    return {
        "model": "trustedrouter/user-dispatch-test",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


def _assert_signed_request(request: httpx.Request, *, owner_stream: bool) -> None:
    assert request.url == "https://owner.example/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {_ENDPOINT_KEY}"
    owner_body = json.loads(request.content)
    assert owner_body["model"] == "owner-upstream"
    assert owner_body["stream"] is owner_stream
    signature = request.headers["TR-Signature"]
    timestamp = int(signature.split(",", 1)[0].removeprefix("t="))
    assert signature == sign_request_body(_SIGNING_FIXTURE, request.content, timestamp)


@pytest.mark.asyncio
async def test_owner_sse_to_requester_stream_passthrough_and_signature(
    test_settings: Settings,
) -> None:
    model = _model(test_settings, supports_streaming=True)

    async def handler(request: httpx.Request) -> httpx.Response:
        _assert_signed_request(request, owner_stream=True)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(),
        )

    body = b"".join(
        [
            chunk
            async for chunk in stream_user_model(
                model,
                _request_body(stream=True),
                test_settings,
                transport=httpx.MockTransport(handler),
            )
        ]
    )
    assert body.count(b'"object":"chat.completion.chunk"') == 2
    assert b"owner " in body and b"stream" in body
    assert body.endswith(b"data: [DONE]\n\n")
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 0


@pytest.mark.asyncio
async def test_owner_sse_to_requester_buffered_aggregates_deltas(
    test_settings: Settings,
) -> None:
    model = _model(test_settings, supports_streaming=True)

    async def handler(request: httpx.Request) -> httpx.Response:
        _assert_signed_request(request, owner_stream=True)
        return httpx.Response(200, content=_sse_body())

    result = await dispatch_user_model(
        model,
        _request_body(stream=False),
        test_settings,
        transport=httpx.MockTransport(handler),
    )
    assert result.body["object"] == "chat.completion"
    assert result.body["model"] == model.id
    assert result.body["choices"][0]["message"]["content"] == "owner stream"
    assert result.first_token_seconds > 0
    assert result.elapsed_seconds >= result.first_token_seconds


@pytest.mark.asyncio
async def test_owner_buffered_to_requester_stream_synthesizes_chunks(
    test_settings: Settings,
) -> None:
    model = _model(test_settings, supports_streaming=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        _assert_signed_request(request, owner_stream=False)
        return httpx.Response(200, json=_buffered_body())

    body = b"".join(
        [
            chunk
            async for chunk in stream_user_model(
                model,
                _request_body(stream=True),
                test_settings,
                transport=httpx.MockTransport(handler),
            )
        ]
    )
    assert body.count(b'"object":"chat.completion.chunk"') == 2
    assert b"buffered reply" in body
    assert b'"finish_reason":"stop"' in body
    assert body.endswith(b"data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_owner_buffered_to_requester_buffered_validates_and_passes_through(
    test_settings: Settings,
) -> None:
    model = _model(test_settings, supports_streaming=False)
    owner_body = _buffered_body()

    async def handler(request: httpx.Request) -> httpx.Response:
        _assert_signed_request(request, owner_stream=False)
        return httpx.Response(200, json=owner_body)

    result = await dispatch_user_model(
        model,
        _request_body(stream=False),
        test_settings,
        transport=httpx.MockTransport(handler),
    )
    assert result.body == owner_body


@pytest.mark.asyncio
async def test_stream_emits_keepalive_before_owner_first_byte(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model(test_settings, supports_streaming=True)
    monkeypatch.setattr(
        "trusted_router.services.user_model_dispatch._KEEPALIVE_SECONDS",
        0.005,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_DelayedStream([_sse_body()], first_delay=0.02),
        )

    body = b"".join(
        [
            chunk
            async for chunk in stream_user_model(
                model,
                _request_body(stream=True),
                test_settings,
                transport=httpx.MockTransport(handler),
            )
        ]
    )
    assert body.startswith(b": keepalive\n\n")
    assert body.endswith(b"data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_timeout_returns_504_and_records_failure(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model(test_settings, supports_streaming=False)

    def tiny_budget(_kind: str) -> Any:
        return type(
            "Budget",
            (),
            {"connect": 1, "first_byte": 0.005, "idle": 1, "total": 1},
        )()

    monkeypatch.setattr(
        "trusted_router.services.user_model_dispatch.dispatch_budget",
        tiny_budget,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_DelayedStream(
                [json.dumps(_buffered_body()).encode()],
                first_delay=0.02,
            ),
        )

    with pytest.raises(HTTPException) as captured:
        await dispatch_user_model(
            model,
            _request_body(stream=False),
            test_settings,
            transport=httpx.MockTransport(handler),
        )
    assert captured.value.status_code == 504
    assert captured.value.detail["error"]["type"] == "user_model_timeout"
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 1


@pytest.mark.asyncio
async def test_malformed_responses_strike_model_offline_and_success_resets(
    test_settings: Settings,
) -> None:
    model = _model(test_settings, supports_streaming=False)

    async def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a completion"})

    for expected_failures in (1, 2):
        with pytest.raises(HTTPException) as captured:
            await dispatch_user_model(
                model,
                _request_body(stream=False),
                test_settings,
                transport=httpx.MockTransport(malformed),
            )
        assert captured.value.status_code == 502
        stored = STORE.get_user_model(model.id)
        assert stored is not None
        assert stored.consecutive_dispatch_failures == expected_failures
        assert stored.online is True

    async def valid(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_buffered_body())

    await dispatch_user_model(
        model,
        _request_body(stream=False),
        test_settings,
        transport=httpx.MockTransport(valid),
    )
    reset = STORE.get_user_model(model.id)
    assert reset is not None
    assert reset.consecutive_dispatch_failures == 0

    for _ in range(3):
        with pytest.raises(HTTPException):
            await dispatch_user_model(
                model,
                _request_body(stream=False),
                test_settings,
                transport=httpx.MockTransport(malformed),
            )
    struck_out = STORE.get_user_model(model.id)
    assert struck_out is not None
    assert struck_out.consecutive_dispatch_failures == 3
    assert struck_out.online is False


@pytest.mark.asyncio
async def test_dispatch_rechecks_dns_and_rejects_private_rebinding(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model(test_settings, supports_streaming=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))
        ],
    )
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_buffered_body())

    with pytest.raises(HTTPException) as captured:
        await dispatch_user_model(
            model,
            _request_body(stream=False),
            test_settings,
            transport=httpx.MockTransport(handler),
        )
    assert captured.value.status_code == 400
    assert called is False
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.consecutive_dispatch_failures == 1


@pytest.mark.asyncio
async def test_stream_timeout_is_sse_error_then_done(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model(test_settings, supports_streaming=True)

    def tiny_budget(_kind: str) -> Any:
        return type(
            "Budget",
            (),
            {"connect": 1, "first_byte": 0.005, "idle": 1, "total": 1},
        )()

    monkeypatch.setattr(
        "trusted_router.services.user_model_dispatch.dispatch_budget",
        tiny_budget,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_DelayedStream([_sse_body()], first_delay=0.02),
        )

    body = b"".join(
        [
            chunk
            async for chunk in stream_user_model(
                model,
                _request_body(stream=True),
                test_settings,
                transport=httpx.MockTransport(handler),
            )
        ]
    )
    assert b"event: error" in body
    assert b'"code":504' in body
    assert b'"type":"user_model_timeout"' in body
    assert body.endswith(b"data: [DONE]\n\n")
