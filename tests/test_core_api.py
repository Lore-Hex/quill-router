from __future__ import annotations

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog import PROVIDER_JURISDICTION_US, PROVIDERS
from trusted_router.storage import STORE


def test_key_create_list_and_one_time_reveal(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    create = client.post("/v1/keys", headers=user_headers, json={"name": "alpha"})
    assert create.status_code == 201
    data = create.json()
    assert data["key"].startswith("sk-tr-v1-")
    assert data["data"]["hash"].startswith("key_")

    listed = client.get("/v1/keys", headers=user_headers)
    assert listed.status_code == 200
    assert "key" not in listed.json()["data"][0]
    assert listed.json()["data"][0]["hash"] == data["data"]["hash"]
    assert "usage_microdollars" in listed.json()["data"][0]

    credits = client.get("/v1/credits", headers=user_headers)
    assert credits.status_code == 200
    credit_data = credits.json()["data"]
    assert isinstance(credit_data["total_credits_microdollars"], int)
    assert isinstance(credit_data["available_microdollars"], int)


def test_inference_key_cannot_call_management_api(
    client: TestClient, inference_headers: dict[str, str]
) -> None:
    resp = client.get("/v1/keys", headers=inference_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_chat_activity_generation_and_no_content_storage(
    client: TestClient,
    inference_headers: dict[str, str],
    user_headers: dict[str, str],
) -> None:
    prompt = "secret prompt should not be stored"
    chat = client.post(
        "/v1/chat/completions",
        headers={**inference_headers, "x-title": "Quill Cloud"},
        json={
            "model": "meta-llama/llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    assert chat.status_code == 200, chat.text
    payload = chat.json()
    assert payload["object"] == "chat.completion"
    assert payload["trustedrouter"]["content_stored"] is False
    assert prompt not in str(payload)
    generation_id = payload["trustedrouter"]["generation_id"]

    activity = client.get("/v1/activity", headers=user_headers)
    assert activity.status_code == 200
    row = activity.json()["data"][0]
    assert row["model"] == "meta-llama/llama-3.1-8b-instruct"
    assert row["provider_name"] == "Cerebras"
    assert row["completion_tokens"] > 0

    events = client.get("/v1/activity?group_by=none", headers=user_headers)
    assert events.status_code == 200
    event = events.json()["data"][0]
    assert event["model"] == "meta-llama/llama-3.1-8b-instruct"
    assert event["app"] == "Quill Cloud"
    assert event["input_tokens"] > 0
    assert event["output_tokens"] > 0
    assert isinstance(event["cost_microdollars"], int)
    assert event["cost_microdollars"] > 0
    assert event["content_stored"] is False
    assert prompt not in str(event)

    generation = client.get(f"/v1/generation?id={generation_id}", headers=user_headers)
    assert generation.status_code == 200
    generation_data = generation.json()["data"]
    assert generation_data["id"] == generation_id
    assert isinstance(generation_data["total_cost_microdollars"], int)
    assert prompt not in str(generation.json())

    # OpenRouter-shaped clients (forty.news, etc.) reuse the SAME bearer
    # token the chat completion was authed with — i.e. an inference key.
    # /v1/generation must accept that, scoped to the workspace owning
    # the generation. Cross-workspace inference keys still 404.
    inference_generation = client.get(
        f"/v1/generation?id={generation_id}", headers=inference_headers
    )
    assert inference_generation.status_code == 200
    assert inference_generation.json()["data"]["id"] == generation_id

    samples = STORE.provider_benchmark_samples()
    assert len(samples) == 1
    sample = samples[0]
    safe_sample = asdict(sample)
    assert sample.status == "success"
    assert sample.model == "meta-llama/llama-3.1-8b-instruct"
    assert sample.provider == "cerebras"
    assert sample.provider_name == "Cerebras"
    assert sample.elapsed_milliseconds is not None
    assert sample.total_cost_microdollars == generation_data["total_cost_microdollars"]
    assert "workspace_id" not in safe_sample
    assert "key_hash" not in safe_sample
    # `app` is the caller's public, opt-in self-reported title (X-Title): it
    # powers the /apps directory and is NOT tenant / credential / prompt data.
    # It is intentionally carried on the benchmark sample; the real privacy
    # guarantees below (no workspace, no key, no prompt body) still hold.
    assert isinstance(safe_sample["app"], str)
    assert prompt not in str(safe_sample)

    content = client.get(f"/v1/generation/content?id={generation_id}", headers=user_headers)
    assert content.status_code == 404
    assert content.json()["error"]["type"] == "content_not_stored"


def test_streaming_chat_uses_sse(client: TestClient, inference_headers: dict[str, str]) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-5.4-nano",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())
    assert b"data: " in body
    assert b"[DONE]" in body


def test_streaming_chat_uses_provider_stream_without_materializing(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    from trusted_router.providers import ProviderClient
    from trusted_router.storage import STORE

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("streaming route must not call ProviderClient.chat")

    def fake_stream_chat(self, model, body, state):
        async def iterator():
            state.request_id = "req_stream_route"
            state.input_tokens = 5
            state.output_tokens = 2
            state.finish_reason = "stop"
            state.usage_estimated = False
            state.record_text("ok")
            yield (
                b'data: {"id":"req_stream_route","choices":[{"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
            )
            yield b"data: [DONE]\n\n"

        return iterator()

    monkeypatch.setattr(ProviderClient, "chat", forbidden_chat)
    monkeypatch.setattr(ProviderClient, "stream_chat", fake_stream_chat)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-5.4-nano",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())

    assert b"req_stream_route" in body
    assert b"[DONE]" in body
    generations = list(STORE.generation_store.generations.values())
    assert len(generations) == 1
    assert generations[0].request_id == "req_stream_route"
    assert generations[0].tokens_prompt == 5
    assert generations[0].tokens_completion == 2
    assert generations[0].streamed is True
    assert generations[0].usage_estimated is False


def test_malformed_json_and_bad_messages_are_stable_errors(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    malformed = client.post(
        "/v1/chat/completions",
        headers={**inference_headers, "content-type": "application/json"},
        content=b"{not json",
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["type"] == "bad_request"
    assert malformed.json()["error"]["source"] == "router"

    bad_role = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-5.4-nano",
            "messages": [{"role": "owner", "content": "hello"}],
        },
    )
    assert bad_role.status_code == 400
    assert bad_role.json()["error"]["type"] == "bad_request"

    missing_content = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={"model": "openai/gpt-5.4-nano", "messages": [{"role": "user"}]},
    )
    assert missing_content.status_code == 400
    assert missing_content.json()["error"]["type"] == "bad_request"

    bad_max_tokens = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-5.4-nano",
            "max_tokens": "a lot",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert bad_max_tokens.status_code == 400
    assert bad_max_tokens.json()["error"]["type"] == "bad_request"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/v1/chat/completions",
            {
                "model": "openai/gpt-5.4-nano",
                "max_tokens": "a lot",
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        (
            "/v1/messages",
            {
                "model": "anthropic/claude-sonnet-4.6",
                "max_tokens": "a lot",
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        (
            "/v1/responses",
            {
                "model": "openai/gpt-5.4-nano",
                "max_output_tokens": "a lot",
                "input": "hello",
            },
        ),
    ],
)
def test_non_integer_output_token_limits_return_bad_request(
    client: TestClient,
    inference_headers: dict[str, str],
    path: str,
    payload: dict[str, object],
) -> None:
    resp = client.post(path, headers=inference_headers, json=payload)

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "bad_request"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/v1/chat/completions",
            {
                "model": "openai/gpt-5.4-nano",
                "max_completion_tokens": "a lot",
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        (
            "/v1/messages",
            {
                "model": "anthropic/claude-sonnet-4.6",
                "max_completion_tokens": "a lot",
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        (
            "/v1/responses",
            {
                "model": "openai/gpt-5.4-nano",
                "max_completion_tokens": "a lot",
                "input": "hello",
            },
        ),
    ],
)
def test_non_integer_max_completion_tokens_returns_bad_request_before_dispatch(
    client: TestClient,
    inference_headers: dict[str, str],
    path: str,
    payload: dict[str, object],
    stream: bool,
) -> None:
    resp = client.post(
        path,
        headers=inference_headers,
        json={**payload, "stream": stream},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "bad_request"
    assert resp.json()["error"]["source"] == "router"
    assert resp.json()["error"]["message"] == "max_completion_tokens must be an integer"


def test_key_limit_validation_uses_stable_errors(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    create = client.post("/v1/keys", headers=user_headers, json={"name": "bad", "limit": "nope"})
    assert create.status_code == 400
    assert create.json()["error"]["type"] == "bad_request"

    key = client.post("/v1/keys", headers=user_headers, json={"name": "ok"}).json()["data"]
    patch = client.patch(f"/v1/keys/{key['hash']}", headers=user_headers, json={"limit": -1})
    assert patch.status_code == 400
    assert patch.json()["error"]["type"] == "bad_request"


def test_provider_errors_map_to_openrouter_style_errors(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    from trusted_router.providers import ProviderClient, ProviderError

    async def boom(*args, **kwargs):
        raise ProviderError("cerebras", 429, "rate limited")

    monkeypatch.setattr(ProviderClient, "chat", boom)
    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "meta-llama/llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "provider_rate_limited"
    assert resp.json()["error"]["source"] == "provider"


def test_provider_errors_do_not_echo_prompt_or_create_generation(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    from trusted_router.providers import ProviderClient, ProviderError
    from trusted_router.storage import STORE

    prompt = "never leak this prompt in an upstream failure"

    async def boom(*_args, **_kwargs):
        raise ProviderError("openai", 503, "upstream unavailable")

    monkeypatch.setattr(ProviderClient, "chat", boom)

    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-5.4-nano",
            "messages": [{"role": "user", "content": prompt}],
        },
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["source"] == "provider"
    assert prompt not in resp.text
    assert STORE.generation_store.generations == {}
    key = next(iter(STORE.api_keys.keys.values()))
    account = STORE.credits[key.workspace_id]
    assert account.total_usage_microdollars == 0
    assert account.reserved_microdollars == 0
    assert key.reserved_microdollars == 0


def test_anthropic_messages_endpoint(client: TestClient, inference_headers: dict[str, str]) -> None:
    resp = client.post(
        "/v1/messages",
        headers=inference_headers,
        json={
            "model": "anthropic/claude-sonnet-4.6",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["type"] == "message"
    assert payload["content"][0]["type"] == "text"
    assert payload["trustedrouter"]["content_stored"] is False


def test_anthropic_messages_stream_uses_provider_stream_without_materializing(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    from trusted_router.providers import ProviderClient
    from trusted_router.storage import STORE

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("streaming Messages route must not call ProviderClient.chat")

    def fake_stream_messages(self, model, body, state):
        async def iterator():
            state.request_id = "msg_stream_route"
            state.input_tokens = 6
            state.output_tokens = 2
            state.finish_reason = "end_turn"
            state.usage_estimated = False
            state.record_text("hi")
            yield (
                b'event: message_start\ndata: {"type":"message_start","message":'
                b'{"id":"msg_stream_route","usage":{"input_tokens":6,"output_tokens":1}}}\n\n'
            )
            yield (
                b'event: content_block_delta\ndata: {"type":"content_block_delta",'
                b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
            )
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        return iterator()

    monkeypatch.setattr(ProviderClient, "chat", forbidden_chat)
    monkeypatch.setattr(ProviderClient, "stream_messages", fake_stream_messages)

    with client.stream(
        "POST",
        "/v1/messages",
        headers=inference_headers,
        json={
            "model": "anthropic/claude-sonnet-4.6",
            "stream": True,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hello"}],
        },
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())

    assert b"event: message_start" in body
    assert b"msg_stream_route" in body
    generations = list(STORE.generation_store.generations.values())
    assert len(generations) == 1
    assert generations[0].request_id == "msg_stream_route"
    assert generations[0].tokens_prompt == 6
    assert generations[0].tokens_completion == 2
    assert generations[0].streamed is True
    assert generations[0].finish_reason == "end_turn"


def test_embeddings_and_model_endpoints(
    client: TestClient, inference_headers: dict[str, str]
) -> None:
    # A chat-only model is not a valid embeddings target.
    not_embeddings = client.post(
        "/v1/embeddings",
        headers=inference_headers,
        json={"model": "openai/gpt-5.5", "input": ["a", "b"]},
    )
    assert not_embeddings.status_code == 400
    assert not_embeddings.json()["error"]["type"] == "model_not_supported"

    # A real embedding model returns the OpenAI embeddings envelope. In the
    # test env enable_live_providers=False, so this exercises the
    # deterministic synthetic vectors — the shape + billing path are real.
    embeddings = client.post(
        "/v1/embeddings",
        headers=inference_headers,
        json={"model": "openai/text-embedding-3-small", "input": ["a", "b"]},
    )
    assert embeddings.status_code == 200, embeddings.text
    payload = embeddings.json()
    assert payload["object"] == "list"
    assert payload["model"] == "openai/text-embedding-3-small"
    assert [row["index"] for row in payload["data"]] == [0, 1]
    assert all(row["object"] == "embedding" and row["embedding"] for row in payload["data"])
    # Embeddings bill input tokens only: total == prompt, no completion.
    assert payload["usage"]["prompt_tokens"] >= 1
    assert payload["usage"]["total_tokens"] == payload["usage"]["prompt_tokens"]
    assert payload["trustedrouter"]["generation_id"]
    assert payload["trustedrouter"]["selected_provider"] == "openai"

    # Empty input is rejected.
    bad = client.post(
        "/v1/embeddings",
        headers=inference_headers,
        json={"model": "openai/text-embedding-3-small", "input": ""},
    )
    assert bad.status_code == 400

    models = client.get("/v1/embeddings/models")
    assert models.status_code == 200
    rows = models.json()["data"]
    embedding_ids = {row["id"] for row in rows}
    assert {"openai/text-embedding-3-large", "cohere/embed-v4.0"} <= embedding_ids
    # OpenAI, Gemini, Together, and Cohere are all represented.
    assert {"openai", "gemini", "together", "cohere"} <= {
        row["trustedrouter"]["provider"] for row in rows
    }
    assert all(row["trustedrouter"]["supports_embeddings"] for row in rows)
    assert all(row["architecture"]["modality"] == "text->embedding" for row in rows)

    endpoint = client.get("/v1/models/meta-llama/llama-3.1-8b-instruct/endpoints")
    assert endpoint.status_code == 200
    endpoint_rows = endpoint.json()["data"]
    # Cerebras no longer serves Llama via Credits on our account (catalog
    # corrected after the dashboard showed it only hosts gpt-oss-120b +
    # glm-4.7). It may still appear as a BYOK route (customer's own key), but
    # must NOT be a prepaid option our key would 502 on.
    assert not [
        row
        for row in endpoint_rows
        if row["provider_name"] == "Cerebras" and row["trustedrouter"]["prepaid_available"]
    ]
    # The model still has working prepaid providers.
    assert any(row["trustedrouter"]["prepaid_available"] for row in endpoint_rows)

    kimi = client.get("/v1/models/moonshotai/kimi-k2.6/endpoints")
    assert kimi.status_code == 200
    assert [item["trustedrouter"]["usage_type"] for item in kimi.json()["data"]][:2] == [
        "Credits",
        "BYOK",
    ]
    assert {item["provider"] for item in kimi.json()["data"]} >= {"kimi", "together"}

    # gpt-5.5 is OpenAI's served flagship (Credits + BYOK on the openai
    # provider). NB: the GPT-5.4 line and the "-pro" tiers are dropped from
    # Credits (closed models OpenAI doesn't serve us — see
    # catalog._UNSERVED_CREDITS_MODELS), so they'd assert BYOK-only.
    openai = client.get("/v1/models/openai/gpt-5.5/endpoints")
    assert openai.status_code == 200
    openai_endpoints = openai.json()["data"]
    # Pin only the canonical first-party provider. Secondary serving
    # providers (gmi, etc.) churn with each OpenRouter snapshot refresh,
    # so asserting their exact membership makes this test flaky against
    # the hourly price-refresh bot.
    assert "openai" in {item["provider"] for item in openai_endpoints}
    assert [
        item["trustedrouter"]["usage_type"]
        for item in openai_endpoints
        if item["provider"] == "openai"
    ] == ["Credits", "BYOK"]

    us_filtered = client.get(
        "/v1/models/z-ai/glm-5.2/endpoints",
        params={"provider[jurisdiction]": "us"},
    )
    assert us_filtered.status_code == 200
    us_rows = us_filtered.json()["data"]
    assert us_rows
    assert all(row["trustedrouter"]["provider_us_based"] is True for row in us_rows)
    assert all(
        PROVIDERS[row["provider"]].provider_headquarters_country == PROVIDER_JURISDICTION_US
        for row in us_rows
    )

    missing = client.get("/v1/models/nope/missing/endpoints")
    assert missing.status_code == 200
    assert missing.json()["data"] == []


def test_responses_api_is_real_and_does_not_store_content(
    client: TestClient,
    inference_headers: dict[str, str],
    user_headers: dict[str, str],
) -> None:
    prompt = "responses private prompt"
    resp = client.post(
        "/v1/responses",
        headers=inference_headers,
        json={
            "model": "openai/gpt-5.4-nano",
            "instructions": "be terse",
            "input": prompt,
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["type"] == "output_text"
    assert payload["trustedrouter"]["content_stored"] is False

    generation_id = payload["trustedrouter"]["generation_id"]
    content = client.get(f"/v1/generation/content?id={generation_id}", headers=user_headers)
    assert content.status_code == 404
    assert content.json()["error"]["type"] == "content_not_stored"


def test_prepaid_insufficient_credits_blocks_before_provider_call(
    client: TestClient,
    inference_headers: dict[str, str],
    user_headers: dict[str, str],
) -> None:
    from trusted_router.storage import STORE

    workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
    STORE.credits[workspace_id].total_credits_microdollars = 0
    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "anthropic/claude-opus-4.7",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["type"] == "insufficient_credits"


def test_api_key_limit_blocks_credit_and_byok_usage(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "limited", "limit": 0.000001, "include_byok_in_limit": True},
    ).json()
    headers = {"authorization": f"Bearer {created['key']}"}
    resp = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "meta-llama/llama-3.1-8b-instruct",
            "provider": {"usage": "byok"},
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["type"] == "key_limit_exceeded"


def test_api_key_limit_can_exclude_byok_usage(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "byok excluded", "limit": 0.000001, "include_byok_in_limit": False},
    ).json()
    headers = {"authorization": f"Bearer {created['key']}"}
    resp = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "meta-llama/llama-3.1-8b-instruct",
            "provider": {"usage": "byok"},
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200
    key_hash = created["data"]["hash"]
    key = client.get(f"/v1/keys/{key_hash}", headers=user_headers).json()["data"]
    assert key["byok_usage"] > 0
    assert key["limit_remaining"] == key["limit"]


def test_disabled_deleted_and_expired_keys_reject(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "disabled"}).json()
    key_hash = created["data"]["hash"]
    headers = {"authorization": f"Bearer {created['key']}"}
    assert (
        client.patch(
            f"/v1/keys/{key_hash}", headers=user_headers, json={"disabled": True}
        ).status_code
        == 200
    )
    disabled = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "openai/gpt-5.4-nano",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert disabled.status_code == 401

    expired = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "expired", "expires_at": "2000-01-01T00:00:00Z"},
    ).json()
    expired_resp = client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {expired['key']}"},
        json={
            "model": "openai/gpt-5.4-nano",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert expired_resp.status_code == 401

    active = client.post("/v1/keys", headers=user_headers, json={"name": "delete me"}).json()
    delete_hash = active["data"]["hash"]
    assert client.delete(f"/v1/keys/{delete_hash}", headers=user_headers).status_code == 200
    deleted = client.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {active['key']}"},
        json={
            "model": "openai/gpt-5.4-nano",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert deleted.status_code == 401


def test_models_providers_credits_and_zdr(client: TestClient, user_headers: dict[str, str]) -> None:
    models = client.get("/v1/models").json()["data"]
    model_ids = {model["id"] for model in models}
    assert models
    assert {
        "trustedrouter/auto",
        "trustedrouter/fast",
        "trustedrouter/eu",
        "trustedrouter/zdr",
        "trustedrouter/e2e",
        "trustedrouter/iris",
        "trustedrouter/prometheus",
        "trustedrouter/zeus",
        "trustedrouter/aristotle",
        "trustedrouter/aristotle-1.0",
        "trustedrouter/aristotle-1.1",
        "trustedrouter/plato",
        "trustedrouter/plato-1.0",
        "trustedrouter/plato-pro",
        "trustedrouter/plato-pro-1.0",
        "trustedrouter/socrates-1.1",
        "trustedrouter/socrates-pro",
        "trustedrouter/socrates-pro-1.0",
        "trustedrouter/socrates-pro-plus",
        "trustedrouter/socrates-pro-plus-1.0",
        "trustedrouter/iris-1.0",
        "trustedrouter/prometheus-1.0",
        "trustedrouter/prometheus-1.0-1m",
        "trustedrouter/zeus-1.0",
        "trustedrouter/zeus-1.0-mini",
        "trustedrouter/iris-code",
        "trustedrouter/prometheus-code",
        "trustedrouter/zeus-code",
        "trustedrouter/iris-code-1.0",
        "trustedrouter/prometheus-code-1.0",
        "trustedrouter/zeus-code-1.0",
        "trustedrouter/openpatcher-g1",
        "trustedrouter/athena",
        "trustedrouter/selector",
        "trustedrouter/mapreduce",
        "google/gemini-3.1-flash-image-preview",
    }.issubset(model_ids)
    models_by_id = {model["id"]: model for model in models}
    fast_meta = models_by_id["trustedrouter/fast"]["trustedrouter"]
    assert fast_meta["route_kind"] == "fast_pool"
    assert fast_meta["auto_candidates"][:4] == [
        "cerebras/gpt-oss-120b",
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "xiaomi/mimo-v2-flash",
        "cerebras/zai-glm-4.7",
    ]
    plato_meta = models_by_id["trustedrouter/plato"]["trustedrouter"]
    plato_pro_meta = models_by_id["trustedrouter/plato-pro-1.0"]["trustedrouter"]
    assert models_by_id["trustedrouter/plato"]["context_length"] == 1_048_576
    assert plato_meta["canonical_model_id"] == "trustedrouter/plato-pro-1.0"
    assert plato_meta["auto_candidates"] == plato_pro_meta["auto_candidates"]
    iris_meta = models_by_id["trustedrouter/iris"]["trustedrouter"]
    assert iris_meta["route_kind"] == "fusion_panel"
    assert iris_meta["auto_candidates"] == [
        "minimax/minimax-m3",
        "moonshotai/kimi-k2.6",
        "deepseek/deepseek-v4-pro",
    ]
    prometheus_code_meta = models_by_id["trustedrouter/prometheus-code"]["trustedrouter"]
    assert prometheus_code_meta["route_kind"] == "fusion_panel"
    assert "moonshotai/kimi-k2.7-code" in prometheus_code_meta["auto_candidates"]
    assert (
        models_by_id["trustedrouter/prometheus-code-1.0"]["trustedrouter"]["auto_candidates"]
        == prometheus_code_meta["auto_candidates"]
    )
    zeus_meta = models_by_id["trustedrouter/zeus"]["trustedrouter"]
    assert models_by_id["trustedrouter/zeus"]["context_length"] == 1_048_576
    assert models_by_id["trustedrouter/zeus-1.0"]["context_length"] == 1_048_576
    assert models_by_id["trustedrouter/zeus-1.0-mini"]["context_length"] == 1_048_576
    assert zeus_meta["auto_candidates"] == [
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.5",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "minimax/minimax-m3",
        "z-ai/glm-5.2",
        "xiaomi/mimo-v2.5-pro",
        "deepseek/deepseek-v4-pro",
    ]
    assert (
        models_by_id["trustedrouter/zeus-1.0"]["trustedrouter"]["auto_candidates"]
        == zeus_meta["auto_candidates"]
    )
    assert models_by_id["trustedrouter/zeus-1.0-mini"]["trustedrouter"]["auto_candidates"] == [
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "minimax/minimax-m3",
        "z-ai/glm-5.2",
        "xiaomi/mimo-v2.5-pro",
        "deepseek/deepseek-v4-pro",
    ]
    aristotle_10_meta = models_by_id["trustedrouter/aristotle-1.0"]["trustedrouter"]
    aristotle_11_meta = models_by_id["trustedrouter/aristotle-1.1"]["trustedrouter"]
    aristotle_meta = models_by_id["trustedrouter/aristotle"]["trustedrouter"]
    assert aristotle_10_meta["route_kind"] == "advisor_orchestration"
    assert aristotle_10_meta["auto_candidates"][:2] == [
        "deepseek/deepseek-v4-flash",
        "anthropic/claude-opus-4.8",
    ]
    assert models_by_id["trustedrouter/aristotle-1.1"]["context_length"] == 1_048_576
    assert models_by_id["trustedrouter/aristotle"]["context_length"] == 1_048_576
    assert aristotle_meta["canonical_model_id"] == "trustedrouter/aristotle-1.1"
    assert aristotle_11_meta["auto_candidates"] == [
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        "trustedrouter/zeus-1.0",
    ]
    assert aristotle_meta["auto_candidates"] == aristotle_11_meta["auto_candidates"]
    socrates_pro_plus_meta = models_by_id["trustedrouter/socrates-pro-plus-1.0"]["trustedrouter"]
    assert socrates_pro_plus_meta["auto_candidates"] == [
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "minimax/minimax-m3",
        "z-ai/glm-5.2-fast",
        "deepseek/deepseek-v4-flash",
        "trustedrouter/zeus-1.0",
    ]
    assert (
        models_by_id["trustedrouter/socrates-1.1"]["trustedrouter"]["auto_candidates"]
        == (socrates_pro_plus_meta["auto_candidates"])
    )
    openpatcher_g1_meta = models_by_id["trustedrouter/openpatcher-g1"]["trustedrouter"]
    assert openpatcher_g1_meta["route_kind"] == "advisor_orchestration"
    assert openpatcher_g1_meta["auto_candidates"] == [
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "trustedrouter/prometheus-1.0-1m",
    ]
    athena_meta = models_by_id["trustedrouter/athena"]["trustedrouter"]
    assert athena_meta["route_kind"] == "private_orchestration"
    assert athena_meta["configuration_hidden"] is True
    assert athena_meta["auto_candidates"] is None
    selector_meta = models_by_id["trustedrouter/selector"]["trustedrouter"]
    mapreduce_meta = models_by_id["trustedrouter/mapreduce"]["trustedrouter"]
    assert selector_meta["route_kind"] == "selector_orchestration"
    assert "moonshotai/kimi-k2.7-code" in selector_meta["auto_candidates"]
    assert mapreduce_meta["route_kind"] == "mapreduce_orchestration"
    assert mapreduce_meta["auto_candidates"][:3] == [
        "deepseek/deepseek-v4-flash",
        "minimax/minimax-m3",
        "cerebras/gpt-oss-120b",
    ]
    # Probe one model from each TR-keyed provider that actually appears
    # in the ingest snapshot. Vertex is intentionally absent — TR doesn't
    # have GCP quota for Anthropic-on-Vertex / Gemini-on-Vertex yet.
    assert {
        "anthropic/claude-opus-4.7",
        "openai/gpt-5.4-nano",
        "google/gemini-2.5-flash",
        "anthropic/claude-fable-5",
        "deepseek/deepseek-v4-flash",
        "moonshotai/kimi-k2.6",
        "mistralai/mistral-small-2603",
        "z-ai/glm-4.6",
        "z-ai/glm-5.2",
    }.issubset(model_ids)
    assert client.get("/v1/models/count").json()["data"]["count"] >= 5
    open_weight_models = client.get("/v1/models", params={"open_weights": "true"})
    assert open_weight_models.status_code == 200
    open_weight_rows = open_weight_models.json()["data"]
    assert open_weight_rows
    assert all(row["trustedrouter"]["open_weights"] is True for row in open_weight_rows)
    assert "trustedrouter/prometheus-1.0" in {row["id"] for row in open_weight_rows}
    assert "trustedrouter/zeus-1.0" not in {row["id"] for row in open_weight_rows}
    us_models = client.get("/v1/models", params={"provider[jurisdiction]": "us"})
    assert us_models.status_code == 200
    us_rows = us_models.json()["data"]
    assert us_rows
    assert all(row["trustedrouter"]["us_provider_available"] is True for row in us_rows)
    assert "trustedrouter/athena" in {row["id"] for row in us_rows}
    eu_models = client.get("/v1/models", params={"provider[region]": "eu"})
    assert eu_models.status_code == 200
    eu_rows = eu_models.json()["data"]
    assert eu_rows
    assert all(row["trustedrouter"]["eu_focused_provider_available"] is True for row in eu_rows)
    assert "trustedrouter/eu" in {row["id"] for row in eu_rows}
    assert client.get("/v1/models/count", params={"open_weights": "true"}).json()["data"][
        "count"
    ] == len(open_weight_rows)
    providers = client.get("/v1/providers").json()["data"]
    provider_flags = {provider["id"]: provider for provider in providers}
    assert [provider["id"] for provider in providers[:2]] == ["tinfoil", "venice"]
    assert {
        "anthropic",
        "openai",
        "gemini",
        "deepseek",
        "kimi",
        "mistral",
        "zai",
        "alibaba",
    }.issubset(provider_flags)
    assert provider_flags["openai"]["supports_prepaid"] is True
    assert provider_flags["alibaba"]["supports_prepaid"] is False
    assert provider_flags["alibaba"]["supports_byok"] is False
    assert provider_flags["deepseek"]["supports_byok"] is True
    assert provider_flags["kimi"]["supports_byok"] is True
    assert provider_flags["mistral"]["supports_byok"] is True
    assert provider_flags["zai"]["supports_byok"] is True
    assert provider_flags["tinfoil"]["provider_e2ee"] is True
    assert provider_flags["phala"]["provider_confidential_compute"] is True
    assert provider_flags["anthropic"]["provider_zero_data_retention"] is False
    assert provider_flags["cerebras"]["provider_zero_data_retention"] is True
    assert provider_flags["together"]["provider_zero_data_retention"] is True
    assert provider_flags["nebius"]["provider_zero_data_retention"] is True
    assert provider_flags["venice"]["provider_zero_data_retention"] is True
    # Venice runs TEE + E2EE inference — it's confidential, not merely no-logs.
    assert provider_flags["venice"]["provider_confidential_compute"] is True
    assert provider_flags["venice"]["provider_e2ee"] is True
    # DeepInfra is memory-only / no-training — earns ZDR with a citation.
    assert provider_flags["deepinfra"]["provider_zero_data_retention"] is True
    assert provider_flags["openai"]["provider_zero_data_retention"] is False
    assert provider_flags["gemini"]["provider_zero_data_retention"] is False
    assert "Not currently marked ZDR" in provider_flags["anthropic"]["provider_policy"]
    assert "Not currently marked ZDR" in provider_flags["openai"]["provider_policy"]
    assert "Not currently marked ZDR" in provider_flags["gemini"]["provider_policy"]
    # GMI runs VPC isolation, NOT an attested TEE — must NOT claim confidential.
    assert provider_flags["gmi"]["provider_confidential_compute"] is None
    assert provider_flags["deepseek"]["provider_zero_data_retention"] is False
    assert "train or improve" in provider_flags["deepseek"]["provider_policy"]
    providers_without_policy_source = [
        provider["id"] for provider in providers if not provider.get("provider_policy_url")
    ]
    assert providers_without_policy_source == []
    expected_policy_sources = {
        "tinfoil": "https://tinfoil.sh/security-and-privacy-faq",
        "venice": "https://docs.venice.ai/overview/privacy",
        "anthropic": "https://platform.claude.com/docs/en/api/data-retention",
        "together": "https://docs.together.ai/docs/privacy-and-security",
        "deepinfra": "https://docs.deepinfra.com/account/data-privacy",
        "nebius": "https://docs.studio.nebius.com/legal/legal-quick-guide",
        "alibaba": "https://www.alibabacloud.com/help/en/model-studio/model-pricing",
        "deepseek": (
            "https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html?locale=en_US"
        ),
    }
    for provider, source in expected_policy_sources.items():
        assert provider_flags[provider]["provider_policy_url"] == source
    zdr = client.get("/v1/endpoints/zdr").json()["data"]
    assert zdr
    zdr_providers = {item["provider"] for item in zdr}
    assert {
        "trustedrouter",
        "cerebras",
        "deepinfra",
        "nebius",
        "phala",
        "tinfoil",
        "together",
        "venice",
    }.issubset(zdr_providers)
    assert "anthropic" not in zdr_providers
    assert "gemini" not in zdr_providers
    assert "openai" not in zdr_providers
    assert "deepseek" not in zdr_providers
    assert "gmi" not in zdr_providers
    credits = client.get("/v1/credits", headers=user_headers)
    assert credits.status_code == 200
    assert credits.json()["data"]["total_credits"] >= 0


def test_byok_provider_config_never_stores_or_returns_raw_key(
    client: TestClient,
    user_headers: dict[str, str],
    test_settings,
) -> None:
    from trusted_router.byok_crypto import decrypt_byok_secret
    from trusted_router.storage import STORE

    raw_key = "csk-test-secret-value-1234"
    upsert = client.put(
        "/v1/byok/providers/cerebras",
        headers=user_headers,
        json={"api_key": raw_key},
    )
    assert upsert.status_code == 201, upsert.text
    payload = upsert.json()["data"]
    assert payload["provider"] == "cerebras"
    assert payload["key_hint"] == "csk-te...1234"
    assert payload["secret_ref"].startswith("byok://")
    assert payload["secret_storage"] == "envelope"  # noqa: S105 - storage kind, not a secret.
    assert raw_key not in str(payload)
    assert raw_key not in str(STORE.byok_store.providers)
    config = (
        STORE.get_byok_provider(payload["workspace_id"], "cerebras")
        if "workspace_id" in payload
        else None
    )
    if config is None:
        workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
        config = STORE.get_byok_provider(workspace_id, "cerebras")
    assert config is not None
    assert config.encrypted_secret is not None
    assert config.secret_ref == payload["secret_ref"]
    assert (
        decrypt_byok_secret(
            config.encrypted_secret,
            test_settings,
            workspace_id=config.workspace_id,
            provider=config.provider,
        )
        == raw_key
    )

    listed = client.get("/v1/byok/providers", headers=user_headers)
    assert listed.status_code == 200
    assert listed.json()["data"][0]["provider"] == "cerebras"
    assert listed.json()["data"][0]["secret_storage"] == "envelope"  # noqa: S105
    assert raw_key not in str(listed.json())

    fetched = client.get("/v1/byok/providers/cerebras", headers=user_headers)
    assert fetched.status_code == 200
    assert fetched.json()["data"]["key_hint"] == "csk-te...1234"
    assert "encrypted_secret" not in str(fetched.json())


def test_byok_provider_config_returns_503_when_kms_encrypt_denied(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch,
) -> None:
    # A management caller can land on a control-plane node whose GCP SA can't
    # encrypt with the byok-envelope KMS key. That must return a clean 503 — never an
    # unhandled 500 + KMS stack trace (prod 2026-06-08).
    from google.api_core import exceptions as gcp_exceptions

    import trusted_router.routes.byok as byok_routes

    def _denied(*_args: object, **_kwargs: object) -> object:
        raise gcp_exceptions.PermissionDenied("useToEncrypt denied on byok-envelope")

    monkeypatch.setattr(byok_routes, "encrypt_byok_secret", _denied)
    resp = client.put(
        "/v1/byok/providers/cerebras",
        headers=user_headers,
        json={"api_key": "csk-test-secret-value-1234"},
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["type"] == "service_unavailable"


def test_byok_registration_refused_when_disabled_on_replica() -> None:
    # Replica nodes (byok_registration_enabled=False) refuse BYOK
    # registration with a clean 503 BEFORE any KMS attempt — a direct hit to a
    # read-only node never 500s.
    from trusted_router.config import Settings
    from trusted_router.main import create_app

    replica = TestClient(create_app(Settings(environment="test", byok_registration_enabled=False)))
    resp = replica.put(
        "/v1/byok/providers/cerebras",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"api_key": "csk-test-secret-value-1234"},
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["type"] == "service_unavailable"
    assert "primary control plane" in resp.json()["error"]["message"]


def test_byok_provider_config_rejects_unsupported_and_raw_secret_refs(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    unsupported = client.put(
        "/v1/byok/providers/vertex",
        headers=user_headers,
        json={"secret_ref": "env://VERTEX_API_KEY"},
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["type"] == "provider_not_supported"

    raw_ref = client.put(
        "/v1/byok/providers/openai",
        headers=user_headers,
        json={"secret_ref": "sk-this-looks-like-a-raw-provider-key"},
    )
    assert raw_ref.status_code == 400
    assert raw_ref.json()["error"]["type"] == "bad_request"


def test_byok_provider_config_can_use_secret_ref_and_delete(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    env_ref = "env://" + "OPENAI_API_KEY"
    create = client.put(
        "/v1/byok/providers/openai",
        headers=user_headers,
        json={"secret_ref": env_ref, "key_hint": "****abcd"},
    )
    assert create.status_code == 201
    assert create.json()["data"]["secret_ref"] == env_ref

    update = client.put(
        "/v1/byok/providers/openai",
        headers=user_headers,
        json={"secret_ref": "projects/example/secrets/openai/versions/latest"},
    )
    assert update.status_code == 200
    assert update.json()["data"]["key_hint"] is None

    delete = client.delete("/v1/byok/providers/openai", headers=user_headers)
    assert delete.status_code == 200
    assert delete.json()["data"]["deleted"] is True

    missing = client.get("/v1/byok/providers/openai", headers=user_headers)
    assert missing.status_code == 404


def test_byok_provider_config_accepts_deepseek_kimi_and_mistral(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    for provider, env_name in [
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("kimi", "KIMI_API_KEY"),
        ("mistral", "MISTRAL_API_KEY"),
    ]:
        create = client.put(
            f"/v1/byok/providers/{provider}",
            headers=user_headers,
            json={"secret_ref": f"env://{env_name}", "key_hint": f"{provider[:3]}...1234"},
        )
        assert create.status_code == 201, create.text
        assert create.json()["data"]["provider"] == provider
        assert create.json()["data"]["secret_ref"] == f"env://{env_name}"


def test_workspaces_and_members(client: TestClient, user_headers: dict[str, str]) -> None:
    create = client.post("/v1/workspaces", headers=user_headers, json={"name": "Org"})
    assert create.status_code == 201
    workspace_id = create.json()["data"]["id"]

    rename = client.patch(
        f"/v1/workspaces/{workspace_id}",
        headers={**user_headers, "x-trustedrouter-workspace": workspace_id},
        json={"name": "Renamed Org"},
    )
    assert rename.status_code == 200
    assert rename.json()["data"]["name"] == "Renamed Org"

    add = client.post(
        f"/v1/workspaces/{workspace_id}/members/add",
        headers={**user_headers, "x-trustedrouter-workspace": workspace_id},
        json={"emails": ["bob@example.com"], "role": "admin"},
    )
    assert add.status_code == 200
    assert add.json()["data"][0]["role"] == "admin"

    members = client.get(
        "/v1/organization/members",
        headers={**user_headers, "x-trustedrouter-workspace": workspace_id},
    )
    assert members.status_code == 200
    assert any(member["email"] == "bob@example.com" for member in members.json()["data"])

    delete = client.delete(
        f"/v1/workspaces/{workspace_id}",
        headers={**user_headers, "x-trustedrouter-workspace": workspace_id},
    )
    assert delete.status_code == 200
    assert delete.json()["data"] == {"deleted": True, "id": workspace_id}
    assert client.get(f"/v1/workspaces/{workspace_id}", headers=user_headers).status_code == 404
