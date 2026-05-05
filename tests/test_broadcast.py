from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from trusted_router.auth import SESSION_COOKIE_NAME
from trusted_router.storage import STORE


def _create_key(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "broadcast@example.com"},
        json={"name": "broadcast key"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


def _console_client(client: TestClient) -> tuple[str, str]:
    user = STORE.ensure_user("console-broadcast@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="console-broadcast",
        ttl_seconds=3600,
        workspace_id=workspace.id,
        state="active",
    )
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return workspace.id, raw_token


def test_broadcast_destination_crud_redacts_secrets(
    client: TestClient,
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url="https://posthog.example/i/v0/e/", json={"status": 1})

    created = client.post(
        "/v1/broadcast/destinations",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "type": "posthog",
            "name": "Product analytics",
            "endpoint": "https://posthog.example",
            "api_key": "phc_secret_project_token",
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["api_key_configured"] is True
    assert "phc_secret_project_token" not in created.text

    listed = client.get(
        "/v1/broadcast/destinations",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["api_key_configured"] is True
    assert "encrypted_api_key" not in listed.text
    assert "phc_secret_project_token" not in str(STORE.broadcast_store.destinations)

    tested = client.post(
        f"/v1/broadcast/destinations/{data['id']}/test",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    assert tested.status_code == 200, tested.text
    payload = json.loads(httpx_mock.get_request().content)
    assert payload["event"] == "$ai_generation"
    assert payload["api_key"] == "phc_secret_project_token"
    assert "$ai_input" not in payload["properties"]
    assert "$ai_output_choices" not in payload["properties"]


def test_webhook_test_accepts_openrouter_style_bad_request(
    client: TestClient,
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url="https://webhook.example/otlp", status_code=400, json={"ok": False})
    created = client.post(
        "/v1/broadcast/destinations",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={
            "type": "webhook",
            "name": "OTLP sink",
            "endpoint": "https://webhook.example/otlp",
            "headers": {"Authorization": "Bearer webhook-secret"},
        },
    )
    assert created.status_code == 201, created.text
    destination_id = created.json()["data"]["id"]

    tested = client.post(
        f"/v1/broadcast/destinations/{destination_id}/test",
        headers={"x-trustedrouter-user": "alice@example.com"},
    )
    assert tested.status_code == 200, tested.text
    request = httpx_mock.get_request()
    assert request.headers["X-Test-Connection"] == "true"
    assert request.headers["Authorization"] == "Bearer webhook-secret"
    assert json.loads(request.content) == {"resourceSpans": []}


def test_gateway_authorize_only_returns_content_enabled_encrypted_destinations(
    client: TestClient,
) -> None:
    key = _create_key(client)
    headers = {"x-trustedrouter-user": "broadcast@example.com"}
    metadata_only = client.post(
        "/v1/broadcast/destinations",
        headers=headers,
        json={
            "type": "webhook",
            "name": "metadata only",
            "endpoint": "https://metadata.example/otlp",
            "headers": {"Authorization": "Bearer metadata"},
        },
    )
    assert metadata_only.status_code == 201, metadata_only.text
    content_enabled = client.post(
        "/v1/broadcast/destinations",
        headers=headers,
        json={
            "type": "webhook",
            "name": "content",
            "endpoint": "https://content.example/otlp",
            "include_content": True,
            "headers": {"Authorization": "Bearer content-secret"},
        },
    )
    assert content_enabled.status_code == 201, content_enabled.text

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-4o-mini",
            "estimated_input_tokens": 100,
            "max_output_tokens": 20,
        },
    )
    assert authorize.status_code == 200, authorize.text
    destinations = authorize.json()["data"]["broadcast_destinations"]
    assert len(destinations) == 1
    destination = destinations[0]
    assert destination["id"] == content_enabled.json()["data"]["id"]
    assert destination["include_content"] is True
    assert destination["encrypted_headers"]["ciphertext"]
    assert "content-secret" not in authorize.text
    assert metadata_only.json()["data"]["id"] not in authorize.text


def test_metadata_broadcast_omits_prompt_and_output(
    client: TestClient,
    httpx_mock: HTTPXMock,
) -> None:
    key = _create_key(client)
    httpx_mock.add_response(url="https://posthog.example/i/v0/e/", json={"status": 1})
    created = client.post(
        "/v1/broadcast/destinations",
        headers={"x-trustedrouter-user": "broadcast@example.com"},
        json={
            "type": "posthog",
            "name": "PostHog metadata",
            "endpoint": "https://posthog.example",
            "api_key": "phc_secret_project_token",
            "include_content": False,
        },
    )
    assert created.status_code == 201, created.text
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-4o-mini",
            "estimated_input_tokens": 12,
            "max_output_tokens": 8,
            "trace": {"trace_id": "trace-123", "experiment": "alpha"},
            "user": "user-1",
            "session_id": "session-1",
            "route_type": "responses",
        },
    )
    assert authorize.status_code == 200, authorize.text

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorize.json()["data"]["authorization_id"],
            "actual_input_tokens": 12,
            "actual_output_tokens": 8,
            "request_id": "resp_test",
            "finish_reason": "stop",
            "elapsed_seconds": 0.5,
            "streamed": False,
            "trace": {"trace_id": "trace-123", "experiment": "alpha"},
            "user": "user-1",
            "session_id": "session-1",
            "route_type": "responses",
        },
    )
    assert settle.status_code == 200, settle.text
    request = httpx_mock.get_request()
    payload = json.loads(request.content)
    assert payload["event"] == "$ai_generation"
    assert payload["distinct_id"] == "user-1"
    assert payload["properties"]["$ai_trace_id"] == "trace-123"
    assert payload["properties"]["$ai_model"] == "openai/gpt-4o-mini"
    assert "$ai_input" not in payload["properties"]
    assert "$ai_output_choices" not in payload["properties"]
    assert "prompt" not in json.dumps(payload).lower()
    assert "output text" not in json.dumps(payload).lower()
    assert all(job.status == "sent" for job in STORE.broadcast_store.delivery_jobs.values())


def test_metadata_broadcast_queues_and_retries_failures(
    client: TestClient,
    httpx_mock: HTTPXMock,
) -> None:
    key = _create_key(client)
    httpx_mock.add_response(url="https://posthog.example/i/v0/e/", status_code=503)
    httpx_mock.add_response(url="https://posthog.example/i/v0/e/", json={"status": 1})
    created = client.post(
        "/v1/broadcast/destinations",
        headers={"x-trustedrouter-user": "broadcast@example.com"},
        json={
            "type": "posthog",
            "name": "PostHog retry",
            "endpoint": "https://posthog.example",
            "api_key": "phc_secret_project_token",
        },
    )
    assert created.status_code == 201, created.text
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "openai/gpt-4o-mini",
            "estimated_input_tokens": 12,
            "max_output_tokens": 8,
        },
    )
    assert authorize.status_code == 200, authorize.text

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": authorize.json()["data"]["authorization_id"],
            "actual_input_tokens": 12,
            "actual_output_tokens": 8,
            "request_id": "resp_retry",
            "elapsed_seconds": 0.5,
        },
    )
    assert settle.status_code == 200, settle.text
    job = next(iter(STORE.broadcast_store.delivery_jobs.values()))
    assert job.status == "pending"
    assert job.attempts == 1
    # Force the retry due in local storage so the internal worker endpoint can
    # drain it deterministically without sleeping.
    job.next_attempt_at = "2000-01-01T00:00:00Z"

    drained = client.post("/v1/internal/broadcast/drain?limit=10")
    assert drained.status_code == 200, drained.text
    assert STORE.broadcast_store.delivery_jobs[job.id].status == "sent"
    assert len(httpx_mock.get_requests()) == 2


def test_console_broadcast_page_creates_destination(client: TestClient) -> None:
    workspace_id, _ = _console_client(client)
    page = client.get("/console/broadcast")
    assert page.status_code == 200, page.text
    assert "Include prompt and output content" in page.text

    created = client.post(
        "/console/broadcast",
        data={
            "name": "Console webhook",
            "destination_type": "webhook",
            "endpoint": "https://console-webhook.example/otlp",
            "method": "POST",
            "headers_json": '{"Authorization":"Bearer console-secret"}',
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    destinations = STORE.list_broadcast_destinations(workspace_id)
    assert len(destinations) == 1
    assert destinations[0].include_content is False
    assert destinations[0].header_names == ["Authorization"]
    assert "console-secret" not in str(STORE.broadcast_store.destinations)
