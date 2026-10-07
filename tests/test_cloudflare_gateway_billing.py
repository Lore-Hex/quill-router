"""Cloudflare inference must use the funded gateway without retaining content."""

import json
from dataclasses import replace

import httpx
import pytest

from trusted_router.catalog import Model
from trusted_router.providers import ProviderClient
from trusted_router.secrets import LocalKeyFile

MODEL = Model(
    id="z-ai/glm-5.3-flash",
    name="GLM 5.3 Flash",
    provider="cloudflare-workers-ai",
    context_length=1_048_576,
    upstream_id="@cf/zai-org/glm-5.3-flash",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cloudflare_chat_uses_private_prepaid_gateway(tmp_path, monkeypatch, stream):
    key_file = tmp_path / "keys.private"
    key_file.write_text(
        "CLOUDFLARE_WORKERS_AI_API_TOKEN=test-token\n"
        "CLOUDFLARE_WORKERS_AI_ACCOUNT_ID=test-account\n",
    )
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == (
            "https://api.cloudflare.com/client/v4/accounts/test-account/ai/v1/chat/completions"
        )
        assert request.headers["authorization"] == "Bearer test-token"
        for header, value in {
            "cf-aig-gateway-id": "default",
            "cf-aig-collect-log": "false",
            "cf-aig-collect-log-payload": "false",
            "cf-aig-skip-cache": "true",
        }.items():
            assert request.headers[header] == value
        body = json.loads(request.content)
        assert body["model"] == MODEL.upstream_id
        assert body["stream"] is stream
        usage = {"prompt_tokens": 17, "completion_tokens": 3, "total_tokens": 20}
        choice = {"finish_reason": "stop", "index": 0}
        choice["delta" if stream else "message"] = {"content": "PONG"}
        payload = {"id": "chatcmpl_cf", "choices": [choice], "usage": usage}
        if stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=payload)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "trusted_router.provider_adapters.httpx.AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    client = ProviderClient(LocalKeyFile(key_file), live=True)
    body = {"messages": [{"role": "user", "content": "Reply PONG."}], "max_tokens": 16}
    if stream:
        state = client.new_stream_state(MODEL, body)
        chunks = [chunk async for chunk in client.stream_chat(MODEL, body, state)]
        assert b"PONG" in b"".join(chunks)
        assert b"[DONE]" in b"".join(chunks)
        result = state.to_result()
    else:
        result = await client.chat(MODEL, body)
    assert result.text == "PONG"
    assert result.input_tokens == 17
    assert result.output_tokens == 3
    assert result.usage_estimated is False
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["openai", "lightning", "zai", "zero-g", "wafer"])
def test_other_providers_do_not_receive_cloudflare_headers(provider):
    headers = ProviderClient._provider_extra_headers(replace(MODEL, provider=provider))
    assert not any(header.startswith("cf-aig-") for header in headers)
