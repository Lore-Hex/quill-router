from __future__ import annotations

from typing import Any

import httpx
import pytest

from trusted_router.catalog import MODELS
from trusted_router.providers import ProviderClient
from trusted_router.secrets import LocalKeyFile


@pytest.mark.asyncio
async def test_mock_provider_path_does_not_require_live_provider_keys(tmp_path) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("", encoding="utf-8")
    client = ProviderClient(LocalKeyFile(key_file), live=False)

    result = await client.chat(
        MODELS["anthropic/claude-3-5-sonnet"],
        {"messages": [{"role": "user", "content": "hello"}]},
    )

    assert result.provider_name == "Anthropic"
    assert result.text == "TrustedRouter response from anthropic/claude-3-5-sonnet."
    assert result.usage_estimated is True


@pytest.mark.asyncio
async def test_live_provider_missing_secret_fails_before_http(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text("", encoding="utf-8")

    class ForbiddenAsyncClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("missing provider credentials should fail before HTTP client creation")

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", ForbiddenAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await client.chat(
            MODELS["openai/gpt-4o-mini"],
            {"messages": [{"role": "user", "content": "hello"}]},
        )


@pytest.mark.asyncio
async def test_live_provider_uses_custom_base_url_from_key_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text(
        "OPENAI_API_KEY=openai-value\nOPENAI_BASE_URL=https://provider-proxy.internal/v7\n",
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
                    "id": "custom_base",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    result = await client.chat(
        MODELS["openai/gpt-4o-mini"],
        {"messages": [{"role": "user", "content": "hello"}]},
    )

    assert result.request_id == "custom_base"
    assert calls[0]["url"] == "https://provider-proxy.internal/v7/chat/completions"
    assert calls[0]["headers"] == {"authorization": "Bearer openai-value"}


def test_vertex_auth_uses_explicit_proxy_without_adc(tmp_path) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text(
        "\n".join(
            [
                "VERTEX_ACCESS_TOKEN=ya29.explicit",
                "VERTEX_PROJECT_ID=trusted-router",
                "VERTEX_LOCATION=europe-west4",
                "VERTEX_OPENAI_BASE_URL=https://vertex-proxy.internal/openai/",
            ]
        ),
        encoding="utf-8",
    )
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    token, base_url = client._vertex_auth_and_base_url()

    assert token == "ya29.explicit"  # noqa: S105 - expected placeholder token.
    assert base_url == "https://vertex-proxy.internal/openai"
