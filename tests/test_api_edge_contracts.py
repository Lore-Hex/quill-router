from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def test_generation_lookup_is_workspace_scoped(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    prompt = "workspace scoped private prompt"
    chat = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    assert chat.status_code == 200, chat.text
    generation_id = chat.json()["trustedrouter"]["generation_id"]

    owner = client.get(
        f"/v1/generation?id={generation_id}",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    stranger = client.get(
        f"/v1/generation?id={generation_id}",
        headers={"x-trustedrouter-user": "mallory@example.com"},
    )
    content = client.get(
        f"/v1/generation/content?id={generation_id}",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )

    assert owner.status_code == 200
    assert owner.json()["data"]["id"] == generation_id
    assert prompt not in owner.text
    assert stranger.status_code == 404
    assert stranger.json()["error"]["type"] == "not_found"
    assert content.status_code == 404
    assert content.json()["error"]["type"] == "content_not_stored"


def test_embeddings_reject_unsupported_model_without_writing_generation(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    resp = client.post(
        "/v1/embeddings",
        headers=inference_headers,
        json={"model": "mistral/mistral-small-2603", "input": "hello"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "model_not_supported"
    assert STORE.generation_store.generations == {}


def test_responses_api_validation_and_metadata_privacy(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    missing_input = client.post(
        "/v1/responses",
        headers=inference_headers,
        json={"model": "openai/gpt-4o-mini"},
    )
    assert missing_input.status_code == 400
    assert missing_input.json()["error"]["type"] == "bad_request"

    prompt = "responses private prompt"
    ok = client.post(
        "/v1/responses",
        headers=inference_headers,
        json={
            "model": "openai/gpt-4o-mini",
            "instructions": "reply tersely",
            "input": [{"type": "message", "role": "user", "content": prompt}],
        },
    )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["object"] == "response"
    assert payload["trustedrouter"]["content_stored"] is False
    assert prompt not in ok.text
    events = client.get(
        "/v1/activity?group_by=none",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    assert events.status_code == 200
    assert prompt not in events.text


def test_chat_completions_accepts_common_openai_sdk_extras_and_router_fields(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    prompt = "compat prompt should not be echoed"
    metadata_marker = "compat metadata should not be echoed"

    response = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={
            "model": "openai/gpt-4o-mini",
            "models": ["mistral/mistral-small-2603", "openai/gpt-4o-mini"],
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
            "tool_choice": "auto",
            "response_format": {"type": "json_object"},
            "metadata": {"trace": metadata_marker},
            "provider": {
                "only": ["mistral", "openai"],
                "ignore": ["anthropic"],
                "sort": "price",
                "allow_fallbacks": True,
            },
            "stream_options": {"include_usage": True},
            "unknown_sdk_field": {"kept_for_forward_compat": True},
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["trustedrouter"]["requested_model"] == "openai/gpt-4o-mini"
    assert payload["trustedrouter"]["selected_model"] in {
        "mistral/mistral-small-2603",
        "openai/gpt-4o-mini",
    }
    assert prompt not in response.text
    assert metadata_marker not in response.text


def test_embeddings_accept_scalar_and_array_inputs_without_generation_rows(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    scalar = client.post(
        "/v1/embeddings",
        headers=inference_headers,
        json={"model": "openai/gpt-4o-mini", "input": "hello"},
    )
    array = client.post(
        "/v1/embeddings",
        headers=inference_headers,
        json={"model": "openai/gpt-4o-mini", "input": ["hello", "world"]},
    )

    assert scalar.status_code == 200, scalar.text
    assert array.status_code == 200, array.text
    assert len(scalar.json()["data"]) == 1
    assert len(array.json()["data"]) == 2
    assert STORE.generation_store.generations == {}
