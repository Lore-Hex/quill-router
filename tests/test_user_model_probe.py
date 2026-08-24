from __future__ import annotations

import json
import socket
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from trusted_router.config import Settings
from trusted_router.services.user_model_probe import probe_user_model
from trusted_router.services.user_model_secrets import (
    encrypt_user_model_endpoint_key,
    encrypt_user_model_signing_secret,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import UserProvidedModel
from trusted_router.user_model_rules import sign_request_body


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )


def _stored_model(settings: Settings, *, supports_streaming: bool = True) -> UserProvidedModel:
    user = STORE.ensure_user("probe-owner@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    signing_secret = "probe-signing-secret"  # noqa: S105 - synthetic crypto fixture
    endpoint_key = "probe-endpoint-key"  # noqa: S105 - synthetic crypto fixture
    return STORE.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Probe model",
        kind="machine",
        display_name="probe-operator",
        endpoint_url="https://owner.example/v1",
        upstream_model_id="upstream-probe",
        encrypted_endpoint_api_key=encrypt_user_model_endpoint_key(
            endpoint_key,
            settings,
            workspace_id=workspace.id,
        ),
        encrypted_signing_secret=encrypt_user_model_signing_secret(
            signing_secret,
            settings,
            workspace_id=workspace.id,
        ),
        supports_streaming=supports_streaming,
        slug="probe-model",
    )


@pytest.mark.asyncio
async def test_probe_uses_only_the_registered_transport_for_a_streaming_model(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    """One leg, streamed, because that is the only shape dispatch ever sends.

    `_owner_request` puts `stream: model.supports_streaming` on every real
    call. A probe that also demanded a buffered `chat.completion` was testing
    a request the endpoint never receives, and it rejected streaming-only
    endpoints — which is exactly what the docs tell owners to build.
    """
    model = _stored_model(test_settings)
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        content=(
            b'data: {"id":"chatcmpl-probe","object":"chat.completion.chunk",'
            b'"choices":[{"delta":{"content":"pong"}}]}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )

    result = await probe_user_model(model, test_settings)

    assert result.ok is True
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "ok"

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["stream"] is True
    assert body["model"] == "upstream-probe"
    assert requests[0].headers["authorization"] == "Bearer probe-endpoint-key"
    signature = requests[0].headers["tr-signature"]
    timestamp = int(signature.split(",", 1)[0].removeprefix("t="))
    assert signature == sign_request_body(
        "probe-signing-secret", requests[0].content, timestamp
    )


@pytest.mark.asyncio
async def test_streaming_only_endpoint_that_refuses_buffered_still_clocks_in(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    """The regression. This endpoint 400s anything with stream=false.

    Under the old two-leg probe it could never clock in, despite being able
    to serve every request production would send it.
    """
    model = _stored_model(test_settings)

    def _only_streams(request: Any) -> httpx.Response:
        if not json.loads(request.content).get("stream"):
            return httpx.Response(400, json={"error": "this endpoint streams"})
        return httpx.Response(
            200,
            content=(
                b'data: {"id":"c","object":"chat.completion.chunk",'
                b'"choices":[{"delta":{"content":"pong"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    httpx_mock.add_callback(_only_streams, method="POST")

    assert (await probe_user_model(model, test_settings)).ok is True


@pytest.mark.asyncio
async def test_buffered_model_is_probed_buffered(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    model = _stored_model(test_settings, supports_streaming=False)
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        json={
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        },
    )

    assert (await probe_user_model(model, test_settings)).ok is True
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert json.loads(requests[0].content)["stream"] is False


@pytest.mark.asyncio
async def test_streaming_model_answering_buffered_is_a_failed_probe(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    """Adapting is our job in one direction only — the owner must honour the
    transport they registered, or the aggregate step has nothing to read."""
    model = _stored_model(test_settings)
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        json={
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        },
    )

    result = await probe_user_model(model, test_settings)

    assert result.ok is False
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "failed"


@pytest.mark.asyncio
async def test_probe_records_failed_for_malformed_owner_body(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    model = _stored_model(test_settings, supports_streaming=False)
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        json={"choices": []},
    )

    result = await probe_user_model(model, test_settings)

    assert result.ok is False
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "failed"


@pytest.mark.asyncio
async def test_probe_rechecks_redirect_target_ip(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _stored_model(test_settings, supports_streaming=False)
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        status_code=307,
        headers={"location": "https://private.example/chat/completions"},
    )

    def resolve(host: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        address = "127.0.0.1" if host == "private.example" else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    result = await probe_user_model(model, test_settings)

    assert result.ok is False
    assert len(httpx_mock.get_requests()) == 1
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "failed"


# --- Review-round regressions -------------------------------------------------


@pytest.mark.asyncio
async def test_probe_strips_credentials_on_cross_origin_redirect(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    """A redirect to another origin must not carry the owner's upstream key
    or a valid TR signature to whoever sits there."""
    model = _stored_model(test_settings, supports_streaming=False)
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        status_code=307,
        headers={"location": "https://elsewhere.example/collect"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://elsewhere.example/collect",
        json={
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        },
    )

    await probe_user_model(model, test_settings)

    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    first, second = requests
    assert first.headers.get("authorization") == "Bearer probe-endpoint-key"
    assert "tr-signature" in first.headers
    assert "authorization" not in second.headers
    assert "tr-signature" not in second.headers


@pytest.mark.asyncio
async def test_probe_keeps_credentials_on_same_origin_redirect(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    model = _stored_model(test_settings, supports_streaming=False)
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        status_code=307,
        headers={"location": "/v2/chat/completions"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v2/chat/completions",
        json={
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        },
    )

    result = await probe_user_model(model, test_settings)

    assert result.ok is True
    second = httpx_mock.get_requests()[1]
    assert second.headers["authorization"] == "Bearer probe-endpoint-key"
    assert "tr-signature" in second.headers


@pytest.mark.asyncio
async def test_probe_caps_response_size(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    """A chatty owner cannot fill control-plane memory through the probe."""
    model = _stored_model(test_settings, supports_streaming=False)
    huge = {
        "id": "chatcmpl-probe",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "x" * (200 * 1024)}}],
    }
    httpx_mock.add_response(
        method="POST",
        url="https://owner.example/v1/chat/completions",
        json=huge,
    )

    result = await probe_user_model(model, test_settings)

    assert result.ok is False
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "failed"


@pytest.mark.asyncio
async def test_probe_has_a_total_deadline_that_bounds_a_trickling_owner(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx's read timeout resets on every byte; a trickling endpoint would
    otherwise hold the probe forever. asyncio.timeout is the real deadline."""
    import asyncio
    from collections.abc import AsyncIterator

    import httpx

    from trusted_router.services import user_model_probe

    monkeypatch.setattr(user_model_probe, "_PROBE_TOTAL_SECONDS", 0.5)
    model = _stored_model(test_settings, supports_streaming=False)

    class _Trickle(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.05)
                yield b" "

        async def aclose(self) -> None:
            return None

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Trickle())

    real_client = httpx.AsyncClient

    def patched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(user_model_probe.httpx, "AsyncClient", patched_client)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await probe_user_model(model, test_settings)
    assert result.ok is False
    assert loop.time() - started < 3.0
    stored = STORE.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "failed"
