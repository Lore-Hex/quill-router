"""Regressions for probe-only failures observed on healthy live routes."""

from __future__ import annotations

import json

import httpx
import pytest

from trusted_router.synthetic.probes import (
    SyntheticTarget,
    provider_rotation_probe,
    provider_throughput_probe,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [
    "meta/muse-spark-1.1", "meta/muse-spark-1.2", "meta/muse-spark-1.3",
    "mistralai/mistral-large-4",
])
@pytest.mark.parametrize("provider", ["meta", "other-host"])
async def test_reasoning_budget_follows_model_not_host(model: str, provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 512
        assert payload["provider"]["only"] == [provider]
        return httpx.Response(200, content=(
            b'data: {"choices":[{"delta":{"content":"PONG"},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_rotation_probe(
            client, SyntheticTarget("rotation", "https://gateway.test/v1", "test"),
            monitor_region="test", api_key="test-key", provider=provider, model=model,
        )
    assert sample.status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("throughput", [False, True])
@pytest.mark.parametrize("provider,model", [
    ("mistral", "mistralai/mistral-large-4"),
    ("mistral", "mistralai/mistral-small-3.2-24b-instruct"),
    ("other-host", "mistralai/mistral-large-4"),
])
async def test_greedy_probe_uses_explicit_valid_nucleus_sampling(
    throughput: bool, provider: str, model: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["temperature"] == 0
        if provider == "mistral":
            assert payload.get("top_p") == 1
        else:
            assert "top_p" not in payload
        return httpx.Response(200, content=(
            b'data: {"choices":[{"delta":{"content":"PONG"},"finish_reason":"stop"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":128}}\n\n'
            b'data: [DONE]\n\n'
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probe = provider_throughput_probe if throughput else provider_rotation_probe
        sample = await probe(
            client, SyntheticTarget("rotation", "https://gateway.test/v1", "test"),
            monitor_region="test", api_key="test-key", provider=provider, model=model,
        )
    assert sample.error_type not in {"probe_config_error", "empty_stream"}
