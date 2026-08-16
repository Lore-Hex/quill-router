from __future__ import annotations

import socket
from typing import Any

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
async def test_probe_validates_buffered_and_streaming_shapes_and_signature(
    test_settings: Settings,
    httpx_mock: HTTPXMock,
) -> None:
    model = _stored_model(test_settings)
    url = "https://owner.example/v1/chat/completions"
    httpx_mock.add_response(
        method="POST",
        url=url,
        json={
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        },
    )
    httpx_mock.add_response(
        method="POST",
        url=url,
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
    assert len(requests) == 2
    for request in requests:
        assert request.headers["authorization"] == "Bearer probe-endpoint-key"
        signature = request.headers["tr-signature"]
        timestamp = int(signature.split(",", 1)[0].removeprefix("t="))
        assert signature == sign_request_body(
            "probe-signing-secret", request.content, timestamp
        )


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
