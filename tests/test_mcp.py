from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS
from trusted_router.config import Settings
from trusted_router.routes import mcp as mcp_routes
from trusted_router.routes.mcp import (
    MAX_MCP_BATCH_ITEMS,
    MAX_MCP_CHAT_BATCH_ITEMS,
    MAX_MCP_EXPENSIVE_BATCH_ITEMS,
)
from trusted_router.storage import InMemoryStore


def _mcp_call(
    client: TestClient,
    name: str,
    arguments: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers=headers or {},
        json={
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tool_json(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload["result"]
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def _batch_call(item_id: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": item_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def test_mcp_initialize_and_tool_list(client: TestClient) -> None:
    initialize = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
    )
    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "trustedrouter"

    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    assert {"models-list", "chat-send", "credits-get", "docs-search"} <= set(tools)
    assert tools["chat-send"]["inputSchema"]["required"] == ["model", "message"]
    for name, tool in tools.items():
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is (name != "chat-send")
        assert annotations["openWorldHint"] is False
        assert annotations["destructiveHint"] is (name == "chat-send")


def test_mcp_models_list_includes_sonnet_5_and_subagent(client: TestClient) -> None:
    sonnet_payload = _mcp_call(client, "models-list", {"query": "sonnet-5", "limit": 5})
    sonnet = _tool_json(sonnet_payload)
    assert "anthropic/claude-sonnet-5" in {
        item["id"] for item in sonnet["data"]
    }

    subagent_payload = _mcp_call(client, "model-get", {"model": "trustedrouter/subagent"})
    subagent = _tool_json(subagent_payload)["data"]
    assert subagent["id"] == "trustedrouter/subagent"
    assert subagent["trustedrouter"]["route_kind"] == "subagent_orchestration"
    assert subagent["trustedrouter"]["byok_available"] is False


def test_mcp_models_hide_internal_monitor_model(client: TestClient) -> None:
    listed_payload = _mcp_call(
        client, "models-list", {"query": "trustedrouter/monitor", "limit": 100}
    )
    listed = _tool_json(listed_payload)
    assert "trustedrouter/monitor" not in {item["id"] for item in listed["data"]}

    fetched = _mcp_call(client, "model-get", {"model": "trustedrouter/monitor"})
    result = fetched["result"]
    assert result["isError"] is True
    assert "Unknown model" in result["content"][0]["text"]


def test_mcp_credits_get_uses_api_key_workspace(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    payload = _mcp_call(client, "credits-get", headers=inference_headers)
    data = _tool_json(payload)["data"]
    assert isinstance(data["workspace_id"], str)
    assert data["workspace_id"]
    assert isinstance(data["available_microdollars"], int)
    assert data["available_microdollars"] > 0


def test_mcp_authenticated_tools_fail_as_tool_errors(client: TestClient) -> None:
    payload = _mcp_call(client, "credits-get")
    result = payload["result"]
    assert result["isError"] is True
    assert "requires Authorization" in result["content"][0]["text"]


def test_mcp_docs_page_is_public(client: TestClient) -> None:
    response = client.get("/docs/mcp")
    assert response.status_code == 200
    assert "https://trustedrouter.com/mcp" in response.text


def test_mcp_rejects_oversized_batch_before_tool_work(
    client: TestClient,
    monkeypatch,
) -> None:
    calls = 0

    def forbidden_models() -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("oversized batch reached catalog work")

    monkeypatch.setattr("trusted_router.routes.mcp.providers_for_display", forbidden_models)
    item = {
        "jsonrpc": "2.0",
        "id": "batch-item",
        "method": "tools/call",
        "params": {"name": "providers-list", "arguments": {}},
    }

    response = client.post("/mcp", json=[item] * (MAX_MCP_BATCH_ITEMS + 1))

    assert response.status_code == 200
    assert "item limit" in response.json()["error"]["message"]
    assert calls == 0


def test_mcp_rejects_nested_batch_without_recursing(client: TestClient) -> None:
    item = {"jsonrpc": "2.0", "id": "ping", "method": "tools/call", "params": {"name": "ping"}}

    response = client.post("/mcp", json=[[item]])

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert "Nested JSON-RPC batches" in payload[0]["error"]["message"]


def test_mcp_rejects_repeated_expensive_tools_before_catalog_work(
    monkeypatch,
) -> None:
    calls = 0

    def forbidden_shape(_model):
        nonlocal calls
        calls += 1
        raise AssertionError("weighted batch reached model rendering")

    monkeypatch.setattr(mcp_routes, "model_to_openrouter_shape", forbidden_shape)
    app = FastAPI()
    mcp_routes.register_mcp_routes(app, Settings(environment="test"))
    mcp_client = TestClient(app)
    batch = [
        _batch_call(str(index), "models-list", {"limit": 1})
        for index in range(MAX_MCP_EXPENSIVE_BATCH_ITEMS + 1)
    ]

    response = mcp_client.post("/mcp", json=batch)

    assert response.status_code == 200
    assert "catalog or documentation calls" in response.json()["error"]["message"]
    assert calls == 0


def test_mcp_caches_catalog_and_documentation_projections_once_per_server(
    monkeypatch,
) -> None:
    model_calls = 0
    provider_catalog_calls = 0
    provider_shape_calls = 0
    docs_calls = 0
    original_model_shape = mcp_routes.model_to_openrouter_shape
    original_providers = mcp_routes.providers_for_display
    original_provider_shape = mcp_routes.provider_to_openrouter_shape
    expected_provider_count = len(original_providers())

    def counted_model_shape(model):
        nonlocal model_calls
        model_calls += 1
        return original_model_shape(model)

    def counted_providers():
        nonlocal provider_catalog_calls
        provider_catalog_calls += 1
        return original_providers()

    def counted_provider_shape(provider):
        nonlocal provider_shape_calls
        provider_shape_calls += 1
        return original_provider_shape(provider)

    def counted_docs(_settings):
        nonlocal docs_calls
        docs_calls += 1
        return "alpha documentation\n\nbeta documentation"

    monkeypatch.setattr(mcp_routes, "model_to_openrouter_shape", counted_model_shape)
    monkeypatch.setattr(mcp_routes, "providers_for_display", counted_providers)
    monkeypatch.setattr(mcp_routes, "provider_to_openrouter_shape", counted_provider_shape)
    monkeypatch.setattr(mcp_routes, "docs_llms_full_txt", counted_docs)
    app = FastAPI()
    mcp_routes.register_mcp_routes(app, Settings(environment="test"))
    mcp_client = TestClient(app)

    def repeated(name: str, arguments: dict[str, Any]) -> None:
        batch = [
            _batch_call(f"{name}-{index}", name, arguments)
            for index in range(MAX_MCP_EXPENSIVE_BATCH_ITEMS)
        ]
        response = mcp_client.post("/mcp", json=batch)
        assert response.status_code == 200
        assert all(item["result"]["isError"] is False for item in response.json())

    repeated("models-list", {"limit": 1})
    repeated("models-list", {"limit": 1})
    repeated("providers-list", {})
    repeated("providers-list", {})
    repeated("docs-search", {"query": "documentation", "limit": 1})
    repeated("docs-search", {"query": "documentation", "limit": 1})

    assert model_calls == len(MODELS)
    assert provider_catalog_calls == 1
    assert provider_shape_calls == expected_provider_count
    assert docs_calls == 1


def test_mcp_rejects_multiple_billable_calls_before_auth_or_network(
    client: TestClient,
    monkeypatch,
) -> None:
    auth_reads = 0

    def forbidden_auth(self, raw: str):
        del self, raw
        nonlocal auth_reads
        auth_reads += 1
        raise AssertionError("multi-chat batch reached authentication")

    class ForbiddenClient:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("multi-chat batch opened an outbound client")

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", forbidden_auth)
    monkeypatch.setattr("trusted_router.routes.mcp.httpx.AsyncClient", ForbiddenClient)
    chat = _batch_call(
        "chat",
        "chat-send",
        {"model": "anthropic/claude-sonnet-5", "message": "hello"},
    )

    response = client.post(
        "/mcp",
        headers={"authorization": "Bearer sk-tr-v1-invalid"},
        json=[chat] * (MAX_MCP_CHAT_BATCH_ITEMS + 1),
    )

    assert response.status_code == 200
    assert "at most one" in response.json()["error"]["message"]
    assert auth_reads == 0


def test_mcp_invalid_bearer_batch_performs_one_auth_read_and_zero_network(
    client: TestClient,
    monkeypatch,
) -> None:
    auth_reads = 0

    def count_invalid(self, raw: str):
        del self
        nonlocal auth_reads
        auth_reads += 1
        assert raw == "sk-tr-v1-invalid"
        return None

    class ForbiddenClient:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("invalid bearer opened an outbound client")

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", count_invalid)
    monkeypatch.setattr("trusted_router.routes.mcp.httpx.AsyncClient", ForbiddenClient)
    batch = [
        _batch_call(
            "chat",
            "chat-send",
            {"model": "anthropic/claude-sonnet-5", "message": "hello"},
        ),
        *[_batch_call(f"credit-{index}", "credits-get") for index in range(15)],
        *[
            _batch_call(f"generation-{index}", "generation-get", {"id": f"gen-{index}"})
            for index in range(16)
        ],
    ]
    assert len(batch) == MAX_MCP_BATCH_ITEMS

    response = client.post(
        "/mcp",
        headers={"authorization": "Bearer sk-tr-v1-invalid"},
        json=batch,
    )

    assert response.status_code == 200
    results = response.json()
    assert len(results) == MAX_MCP_BATCH_ITEMS
    assert all(item["result"]["isError"] is True for item in results)
    assert all(
        "Invalid TrustedRouter API key" in item["result"]["content"][0]["text"]
        for item in results
    )
    assert auth_reads == 1


def test_mcp_valid_authenticated_batch_reuses_one_key_context(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    auth_reads = 0
    original = InMemoryStore.api_key_auth_context

    def count_valid(self, raw: str):
        nonlocal auth_reads
        auth_reads += 1
        return original(self, raw)

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", count_valid)
    batch = [_batch_call(str(index), "credits-get") for index in range(4)]

    response = client.post("/mcp", headers=inference_headers, json=batch)

    assert response.status_code == 200
    assert all(item["result"]["isError"] is False for item in response.json())
    assert auth_reads == 1


def test_mcp_auth_store_failure_is_cached_for_the_batch(
    client: TestClient,
    monkeypatch,
) -> None:
    auth_reads = 0

    def unavailable(self, raw: str):
        del self, raw
        nonlocal auth_reads
        auth_reads += 1
        raise RuntimeError("Store unavailable")

    class ForbiddenClient:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("failed authentication opened an outbound client")

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", unavailable)
    monkeypatch.setattr("trusted_router.routes.mcp.httpx.AsyncClient", ForbiddenClient)
    batch = [
        _batch_call(
            "chat",
            "chat-send",
            {"model": "anthropic/claude-sonnet-5", "message": "hello"},
        ),
        *[_batch_call(str(index), "credits-get") for index in range(10)],
    ]

    response = client.post(
        "/mcp",
        headers={"authorization": "Bearer sk-tr-v1-unavailable"},
        json=batch,
    )

    assert response.status_code == 200
    assert auth_reads == 1
    assert all(
        "authentication is unavailable" in item["result"]["content"][0]["text"]
        for item in response.json()
    )
