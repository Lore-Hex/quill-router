from __future__ import annotations

from dataclasses import asdict

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def test_key_create_list_and_one_time_reveal(client: TestClient, user_headers: dict[str, str]) -> None:
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


def test_inference_key_cannot_call_management_api(client: TestClient, inference_headers: dict[str, str]) -> None:
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
    assert "app" not in safe_sample
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
            "model": "openai/gpt-4o-mini",
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
            "model": "openai/gpt-4o-mini",
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

    bad_role = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "owner", "content": "hello"}],
        },
    )
    assert bad_role.status_code == 400
    assert bad_role.json()["error"]["type"] == "bad_request"

    missing_content = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user"}]},
    )
    assert missing_content.status_code == 400
    assert missing_content.json()["error"]["type"] == "bad_request"

    bad_max_tokens = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-4o-mini",
            "max_tokens": "a lot",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert bad_max_tokens.status_code == 400
    assert bad_max_tokens.json()["error"]["type"] == "bad_request"


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
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
        },
    )

    assert resp.status_code == 502
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


def test_embeddings_and_model_endpoints(client: TestClient, inference_headers: dict[str, str]) -> None:
    embeddings = client.post(
        "/v1/embeddings",
        headers=inference_headers,
        json={"model": "openai/gpt-4o-mini", "input": ["a", "b"]},
    )
    assert embeddings.status_code == 501
    assert embeddings.json()["error"]["type"] == "endpoint_not_supported"

    models = client.get("/v1/embeddings/models")
    assert models.status_code == 200
    # The catalog is sourced entirely from the OpenRouter ingest snapshot,
    # which doesn't carry a `supports_embeddings` flag (OpenRouter focuses on
    # chat/completion). Until embeddings models are added back as a
    # hand-curated subset, `/v1/embeddings/models` returns an empty list —
    # the embeddings *route* still answers (501 above) so callers get a
    # clean error path; no model just means no eligible candidates.
    assert models.json()["data"] == []

    endpoint = client.get("/v1/models/meta-llama/llama-3.1-8b-instruct/endpoints")
    assert endpoint.status_code == 200
    assert endpoint.json()["data"][0]["provider_name"] == "Cerebras"

    kimi = client.get("/v1/models/moonshotai/kimi-k2.6/endpoints")
    assert kimi.status_code == 200
    assert [item["trustedrouter"]["usage_type"] for item in kimi.json()["data"]] == [
        "Credits",
        "BYOK",
    ]

    openai = client.get("/v1/models/openai/gpt-4o-mini/endpoints")
    assert openai.status_code == 200
    assert [item["trustedrouter"]["usage_type"] for item in openai.json()["data"]] == [
        "Credits",
        "BYOK",
    ]

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
            "model": "openai/gpt-4o-mini",
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
    assert client.patch(f"/v1/keys/{key_hash}", headers=user_headers, json={"disabled": True}).status_code == 200
    disabled = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "openai/gpt-4o-mini",
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
            "model": "openai/gpt-4o-mini",
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
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert deleted.status_code == 401


def test_models_providers_credits_and_zdr(client: TestClient, user_headers: dict[str, str]) -> None:
    models = client.get("/v1/models").json()["data"]
    model_ids = {model["id"] for model in models}
    assert models
    # Probe one model from each TR-keyed provider that actually appears
    # in the ingest snapshot. Vertex is intentionally absent — TR doesn't
    # have GCP quota for Anthropic-on-Vertex / Gemini-on-Vertex yet.
    assert {
        "anthropic/claude-opus-4.7",
        "openai/gpt-4o-mini",
        "google/gemini-2.5-flash",
        "deepseek/deepseek-v4-flash",
        "moonshotai/kimi-k2.6",
        "mistralai/mistral-small-2603",
        "z-ai/glm-4.6",
    }.issubset(model_ids)
    assert client.get("/v1/models/count").json()["data"]["count"] >= 5
    providers = client.get("/v1/providers").json()["data"]
    provider_flags = {provider["id"]: provider for provider in providers}
    assert {"anthropic", "openai", "gemini", "deepseek", "kimi", "mistral", "zai"}.issubset(
        provider_flags
    )
    assert provider_flags["openai"]["supports_prepaid"] is True
    assert provider_flags["deepseek"]["supports_byok"] is True
    assert provider_flags["kimi"]["supports_byok"] is True
    assert provider_flags["mistral"]["supports_byok"] is True
    assert provider_flags["zai"]["supports_byok"] is True
    assert client.get("/v1/endpoints/zdr").json()["data"]
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
    config = STORE.get_byok_provider(payload["workspace_id"], "cerebras") if "workspace_id" in payload else None
    if config is None:
        workspace_id = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]["id"]
        config = STORE.get_byok_provider(workspace_id, "cerebras")
    assert config is not None
    assert config.encrypted_secret is not None
    assert config.secret_ref == payload["secret_ref"]
    assert decrypt_byok_secret(
        config.encrypted_secret,
        test_settings,
        workspace_id=config.workspace_id,
        provider=config.provider,
    ) == raw_key

    listed = client.get("/v1/byok/providers", headers=user_headers)
    assert listed.status_code == 200
    assert listed.json()["data"][0]["provider"] == "cerebras"
    assert listed.json()["data"][0]["secret_storage"] == "envelope"  # noqa: S105
    assert raw_key not in str(listed.json())

    fetched = client.get("/v1/byok/providers/cerebras", headers=user_headers)
    assert fetched.status_code == 200
    assert fetched.json()["data"]["key_hint"] == "csk-te...1234"
    assert "encrypted_secret" not in str(fetched.json())


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
