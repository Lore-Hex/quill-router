from __future__ import annotations

from typing import Any

import httpx
import pytest

from trusted_router.adapter import (
    messages_to_chat_body,
    resolve_max_output_tokens,
    responses_to_chat_body,
)
from trusted_router.catalog import MODELS
from trusted_router.provider_adapters import openai_compatible_chat
from trusted_router.provider_payloads import anthropic_messages_payload, gemini_payload
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.inference import _estimate_reserve


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"max_tokens": 100}, 100),
        ({"max_completion_tokens": 200}, 200),
        ({"max_output_tokens": 300}, 300),
        ({}, None),
    ],
)
def test_resolve_max_output_tokens_accepts_all_spellings(
    body: dict[str, int], expected: int | None
) -> None:
    assert resolve_max_output_tokens(body) == expected


def test_resolve_max_output_tokens_precedence() -> None:
    assert (
        resolve_max_output_tokens({
            "max_tokens": 100,
            "max_completion_tokens": 200,
            "max_output_tokens": 300,
        })
        == 100
    )
    assert (
        resolve_max_output_tokens({
            "max_completion_tokens": 200,
            "max_output_tokens": 300,
        })
        == 200
    )


def test_gateway_authorize_request_uses_max_completion_tokens_for_output_estimate() -> None:
    body = GatewayAuthorizeRequest(
        api_key_hash="hash",
        model="openai/gpt-5.4-nano",
        max_completion_tokens=8000,
    )

    assert body.output_estimate == 8000


def test_gateway_authorize_request_prefers_max_tokens_for_output_estimate() -> None:
    body = GatewayAuthorizeRequest(
        api_key_hash="hash",
        model="openai/gpt-5.4-nano",
        max_tokens=100,
        max_completion_tokens=8000,
    )

    assert body.output_estimate == 100


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_completion_tokens", 12_000),
        ("max_output_tokens", 13_000),
    ],
)
def test_estimate_reserve_uses_output_token_aliases(field: str, value: int) -> None:
    model = MODELS["anthropic/claude-sonnet-4.6"]
    input_estimate = 37
    body = {
        "messages": [{"role": "user", "content": "hello"}],
        field: value,
    }

    reserve = _estimate_reserve(body, model, input_estimate=input_estimate)

    assert reserve == cost_microdollars(model, input_estimate, value)
    assert reserve != cost_microdollars(model, input_estimate, 512)


def test_adapter_conversions_use_max_completion_tokens() -> None:
    chat_body = responses_to_chat_body({
        "model": "openai/gpt-5.4-nano",
        "input": "hello",
        "max_completion_tokens": 1234,
    })
    messages_body = messages_to_chat_body(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 2345,
        },
        model_id="anthropic/claude-sonnet-4.6",
    )

    assert chat_body["max_tokens"] == 1234
    assert messages_body["max_tokens"] == 2345


def test_provider_payloads_use_max_completion_tokens() -> None:
    request = {
        "model": "google/gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "max_completion_tokens": 3456,
    }

    anthropic = anthropic_messages_payload(
        MODELS["anthropic/claude-sonnet-4.6"], request, stream=False
    )
    gemini = gemini_payload(request)

    assert anthropic["max_tokens"] == 3456
    assert gemini["generationConfig"]["maxOutputTokens"] == 3456


@pytest.mark.asyncio
async def test_openai_compatible_payload_uses_max_completion_tokens(monkeypatch) -> None:
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
            headers: dict[str, str],
            json: dict[str, Any],
            **_: Any,
        ) -> httpx.Response:
            calls.append({"url": url, "headers": headers, "json": json})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_alias",
                    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)

    await openai_compatible_chat(
        MODELS["openai/gpt-5.4-nano"],
        {
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 4567,
        },
        api_key="openai-value",
        base_url="https://example.test/v1",
    )

    assert calls[0]["json"]["max_tokens"] == 4567
