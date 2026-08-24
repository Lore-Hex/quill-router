from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import httpx
from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS
from trusted_router.routes import mcp as mcp_routes
from trusted_router.routes.mcp import (
    MAX_MCP_BATCH_ITEMS,
    MAX_MCP_CHAT_BATCH_ITEMS,
    MAX_MCP_CHAT_MESSAGE_BYTES,
    MAX_MCP_CHAT_MESSAGE_CHARS,
    MAX_MCP_EXPENSIVE_BATCH_ITEMS,
    MAX_MCP_GENERATION_ID_CHARS,
    MAX_MCP_MODEL_CHARS,
    MAX_MCP_SEARCH_QUERY_CHARS,
    MAX_MCP_STORAGE_BATCH_ITEMS,
)
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import RateLimitHit
from trusted_router.storage_rate_limits import InMemoryRateLimits


def _mcp_call(
    client: TestClient,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers=headers,
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


def test_mcp_rejects_anonymous_initialize_list_and_catalog_before_work(
    client: TestClient,
    monkeypatch,
) -> None:
    def forbidden_tools() -> list[dict[str, Any]]:
        raise AssertionError("anonymous request reached MCP tool listing")

    def forbidden_shape(_model: object) -> dict[str, object]:
        raise AssertionError("anonymous request reached catalog rendering")

    monkeypatch.setattr(mcp_routes, "_mcp_tools", forbidden_tools)
    monkeypatch.setattr(mcp_routes, "model_to_openrouter_shape", forbidden_shape)
    payloads = [
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}},
        _batch_call("catalog", "models-list", {"limit": 1}),
    ]

    for payload in payloads:
        response = client.post("/mcp", json=payload)
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "unauthorized"


def test_mcp_authenticates_before_parsing_json(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    original_auth = InMemoryStore.api_key_auth_context

    def forbidden_auth(self: object, raw: str) -> None:
        del self, raw
        raise AssertionError("missing bearer reached API-key storage")

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", forbidden_auth)
    anonymous = client.post(
        "/mcp",
        headers={"content-type": "application/json"},
        content=b'{"jsonrpc":',
    )
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["type"] == "unauthorized"

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", original_auth)
    authenticated = client.post(
        "/mcp",
        headers={**inference_headers, "content-type": "application/json"},
        content=b'{"jsonrpc":',
    )
    assert authenticated.status_code == 400
    assert authenticated.json()["error"]["message"] == "Parse error"


def test_mcp_rejects_non_api_key_bearers_without_auth_context_reads(
    client: TestClient,
    monkeypatch,
) -> None:
    auth_reads = 0

    def counted_auth(self: object, raw: str) -> None:
        del self, raw
        nonlocal auth_reads
        auth_reads += 1

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", counted_auth)

    for bearer in ("not-a-key", "trsess-v1-session-token", "sk-tr-"):
        response = client.post(
            "/mcp",
            headers={"authorization": f"Bearer {bearer}"},
            json={"jsonrpc": "2.0", "id": "init", "method": "initialize"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["type"] == "unauthorized"

    assert auth_reads == 0


def test_mcp_initialize_and_tool_list(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    initialize = client.post(
        "/mcp",
        headers=inference_headers,
        json={"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
    )
    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "trustedrouter"

    listed = client.post(
        "/mcp",
        headers=inference_headers,
        json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    assert {"models-list", "chat-send", "credits-get", "docs-search"} <= set(tools)
    assert tools["chat-send"]["inputSchema"]["required"] == ["model", "message"]
    assert (
        tools["chat-send"]["inputSchema"]["properties"]["message"]["maxLength"]
        == MAX_MCP_CHAT_MESSAGE_CHARS
    )
    expected_string_limits = {
        ("models-list", "query"): MAX_MCP_SEARCH_QUERY_CHARS,
        ("model-get", "model"): MAX_MCP_MODEL_CHARS,
        ("model-endpoints", "model"): MAX_MCP_MODEL_CHARS,
        ("generation-get", "id"): MAX_MCP_GENERATION_ID_CHARS,
        ("docs-search", "query"): MAX_MCP_SEARCH_QUERY_CHARS,
        ("chat-send", "model"): MAX_MCP_MODEL_CHARS,
    }
    for (tool_name, property_name), maximum in expected_string_limits.items():
        assert (
            tools[tool_name]["inputSchema"]["properties"][property_name]["maxLength"]
            == maximum
        )
    for name, tool in tools.items():
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is (name != "chat-send")
        assert annotations["openWorldHint"] is False
        assert annotations["destructiveHint"] is (name == "chat-send")


def test_mcp_models_list_includes_sonnet_5_and_subagent(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    sonnet_payload = _mcp_call(
        client,
        "models-list",
        {"query": "sonnet-5", "limit": 5},
        headers=inference_headers,
    )
    sonnet = _tool_json(sonnet_payload)
    assert "anthropic/claude-sonnet-5" in {
        item["id"] for item in sonnet["data"]
    }

    subagent_payload = _mcp_call(
        client,
        "model-get",
        {"model": "trustedrouter/subagent"},
        headers=inference_headers,
    )
    subagent = _tool_json(subagent_payload)["data"]
    assert subagent["id"] == "trustedrouter/subagent"
    assert subagent["trustedrouter"]["route_kind"] == "subagent_orchestration"
    assert subagent["trustedrouter"]["byok_available"] is False


def test_mcp_models_hide_internal_monitor_model(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    listed_payload = _mcp_call(
        client,
        "models-list",
        {"query": "trustedrouter/monitor", "limit": 100},
        headers=inference_headers,
    )
    listed = _tool_json(listed_payload)
    assert "trustedrouter/monitor" not in {item["id"] for item in listed["data"]}

    fetched = _mcp_call(
        client,
        "model-get",
        {"model": "trustedrouter/monitor"},
        headers=inference_headers,
    )
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


def test_mcp_docs_page_is_public(client: TestClient) -> None:
    response = client.get("/docs/mcp")
    assert response.status_code == 200
    assert "Every MCP request requires" in response.text
    assert "Public lookup tools work without a key" not in response.text
    assert "https://trustedrouter.com/mcp" in response.text


def test_mcp_rejects_oversized_batch_before_tool_work(
    client: TestClient,
    inference_headers: dict[str, str],
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

    response = client.post(
        "/mcp",
        headers=inference_headers,
        json=[item] * (MAX_MCP_BATCH_ITEMS + 1),
    )

    assert response.status_code == 200
    assert "item limit" in response.json()["error"]["message"]
    assert calls == 0


def test_mcp_rejects_nested_batch_without_recursing(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    item = {"jsonrpc": "2.0", "id": "ping", "method": "tools/call", "params": {"name": "ping"}}

    response = client.post("/mcp", headers=inference_headers, json=[[item]])

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert "Nested JSON-RPC batches" in payload[0]["error"]["message"]


def test_mcp_rejects_repeated_expensive_tools_before_catalog_work(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    calls = 0

    def forbidden_shape(_model):
        nonlocal calls
        calls += 1
        raise AssertionError("weighted batch reached model rendering")

    monkeypatch.setattr(mcp_routes, "model_to_openrouter_shape", forbidden_shape)
    batch = [
        _batch_call(str(index), "models-list", {"limit": 1})
        for index in range(MAX_MCP_EXPENSIVE_BATCH_ITEMS + 1)
    ]

    response = client.post("/mcp", headers=inference_headers, json=batch)

    assert response.status_code == 200
    assert "catalog or documentation calls" in response.json()["error"]["message"]
    assert calls == 0


def test_mcp_rejects_repeated_storage_tools_after_one_authentication(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    auth_reads = 0
    original_auth = InMemoryStore.api_key_auth_context

    def counted_auth(self, raw: str):
        nonlocal auth_reads
        auth_reads += 1
        return original_auth(self, raw)

    def forbidden_storage(_workspace_id: str):
        raise AssertionError("storage-heavy batch reached tool storage")

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", counted_auth)
    monkeypatch.setattr(mcp_routes, "live_credit_summary", forbidden_storage)
    batch = [
        _batch_call(str(index), "credits-get")
        for index in range(MAX_MCP_STORAGE_BATCH_ITEMS + 1)
    ]

    response = client.post(
        "/mcp",
        headers=inference_headers,
        json=batch,
    )

    assert response.status_code == 200
    assert "storage-backed calls" in response.json()["error"]["message"]
    assert auth_reads == 1


def test_mcp_caches_catalog_and_documentation_projections_once_per_server(
    client: TestClient,
    inference_headers: dict[str, str],
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
    def repeated(name: str, arguments: dict[str, Any]) -> None:
        batch = [
            _batch_call(f"{name}-{index}", name, arguments)
            for index in range(MAX_MCP_EXPENSIVE_BATCH_ITEMS)
        ]
        response = client.post("/mcp", headers=inference_headers, json=batch)
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


def test_mcp_rejects_multiple_billable_calls_after_one_auth_and_before_network(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    auth_reads = 0
    original_auth = InMemoryStore.api_key_auth_context

    def counted_auth(self, raw: str):
        nonlocal auth_reads
        auth_reads += 1
        return original_auth(self, raw)

    class ForbiddenClient:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("multi-chat batch opened an outbound client")

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", counted_auth)
    monkeypatch.setattr("trusted_router.routes.mcp.httpx.AsyncClient", ForbiddenClient)
    chat = _batch_call(
        "chat",
        "chat-send",
        {"model": "anthropic/claude-sonnet-5", "message": "hello"},
    )

    response = client.post(
        "/mcp",
        headers=inference_headers,
        json=[chat] * (MAX_MCP_CHAT_BATCH_ITEMS + 1),
    )

    assert response.status_code == 200
    assert "at most one" in response.json()["error"]["message"]
    assert auth_reads == 1


def test_mcp_rejects_oversized_chat_messages_before_network(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    network_clients = 0

    class ForbiddenClient:
        def __init__(self, **_kwargs: Any) -> None:
            nonlocal network_clients
            network_clients += 1
            raise AssertionError("oversized MCP chat opened an outbound client")

    monkeypatch.setattr("trusted_router.routes.mcp.httpx.AsyncClient", ForbiddenClient)
    byte_limited_message = "🙂" * (MAX_MCP_CHAT_MESSAGE_BYTES // 4 + 1)
    assert len(byte_limited_message) <= MAX_MCP_CHAT_MESSAGE_CHARS
    assert len(byte_limited_message.encode("utf-8")) > MAX_MCP_CHAT_MESSAGE_BYTES
    oversized_messages = [
        "x" * (MAX_MCP_CHAT_MESSAGE_CHARS + 1),
        byte_limited_message,
    ]

    for message in oversized_messages:
        response = client.post(
            "/mcp",
            headers=inference_headers,
            json=_batch_call(
                "chat",
                "chat-send",
                {"model": "anthropic/claude-sonnet-5", "message": message},
            ),
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True
        assert "input limit" in result["content"][0]["text"]

    assert network_clients == 0


def test_mcp_rejects_invalid_catalog_and_search_strings_before_scanning(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    catalog_scans = 0
    documentation_scans = 0

    def forbidden_shape(_model: object) -> dict[str, object]:
        nonlocal catalog_scans
        catalog_scans += 1
        raise AssertionError("invalid bounded string reached catalog rendering")

    def forbidden_docs(_settings: object) -> str:
        nonlocal documentation_scans
        documentation_scans += 1
        raise AssertionError("invalid bounded string reached documentation rendering")

    monkeypatch.setattr(mcp_routes, "model_to_openrouter_shape", forbidden_shape)
    monkeypatch.setattr(mcp_routes, "docs_llms_full_txt", forbidden_docs)
    invalid_calls: list[tuple[str, dict[str, Any]]] = [
        ("models-list", {"query": "x" * (MAX_MCP_SEARCH_QUERY_CHARS + 1)}),
        ("models-list", {"query": None}),
        ("model-get", {"model": "x" * (MAX_MCP_MODEL_CHARS + 1)}),
        ("model-get", {"model": 123}),
        ("model-endpoints", {"model": "x" * (MAX_MCP_MODEL_CHARS + 1)}),
        ("model-endpoints", {"model": ["anthropic/claude-sonnet-5"]}),
        ("docs-search", {"query": "x" * (MAX_MCP_SEARCH_QUERY_CHARS + 1)}),
        ("docs-search", {"query": {"contains": "docs"}}),
    ]

    for name, arguments in invalid_calls:
        payload = _mcp_call(client, name, arguments, headers=inference_headers)
        assert payload["result"]["isError"] is True

    assert catalog_scans == 0
    assert documentation_scans == 0


def test_mcp_rejects_invalid_generation_ids_before_storage(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    generation_reads = 0

    def forbidden_generation_read(
        _store: InMemoryStore,
        _generation_id: str,
    ) -> None:
        nonlocal generation_reads
        generation_reads += 1
        raise AssertionError("invalid generation ID reached storage")

    # Patch the concrete store class, never the STORE proxy instance. Restoring
    # an instance monkeypatch would leave a method bound to the old test store
    # on the proxy and make later tests read from the wrong generation store.
    monkeypatch.setattr(InMemoryStore, "get_generation", forbidden_generation_read)
    invalid_ids: list[object] = [
        "x" * (MAX_MCP_GENERATION_ID_CHARS + 1),
        123,
        None,
    ]

    for generation_id in invalid_ids:
        payload = _mcp_call(
            client,
            "generation-get",
            {"id": generation_id},
            headers=inference_headers,
        )
        assert payload["result"]["isError"] is True

    assert generation_reads == 0


def test_mcp_rejects_invalid_chat_models_before_network(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    network_clients = 0

    class ForbiddenClient:
        def __init__(self, **_kwargs: Any) -> None:
            nonlocal network_clients
            network_clients += 1
            raise AssertionError("invalid MCP model opened an outbound client")

    monkeypatch.setattr("trusted_router.routes.mcp.httpx.AsyncClient", ForbiddenClient)
    invalid_models: list[object] = [
        "x" * (MAX_MCP_MODEL_CHARS + 1),
        123,
        None,
    ]

    for model in invalid_models:
        payload = _mcp_call(
            client,
            "chat-send",
            {"model": model, "message": "hello"},
            headers=inference_headers,
        )
        assert payload["result"]["isError"] is True

    assert network_clients == 0


async def _wait_for_blocked_worker_and_heartbeat(started: threading.Event) -> None:
    worker_started = await asyncio.wait_for(asyncio.to_thread(started.wait, 2.0), timeout=3.0)
    assert worker_started, "MCP storage work never reached its worker"
    heartbeats = 0
    for _ in range(5):
        await asyncio.sleep(0.005)
        heartbeats += 1
    assert heartbeats == 5


def test_mcp_credit_lookup_runs_off_the_event_loop(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []

    def blocking_summary(_workspace_id: str) -> dict[str, int]:
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2.0), "MCP credit worker was never released"
        return {
            "total_credits": 10,
            "total_usage": 2,
            "reserved": 1,
            "available": 7,
        }

    monkeypatch.setattr(mcp_routes, "live_credit_summary", blocking_summary)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            request = asyncio.create_task(
                ac.post(
                    "/mcp",
                    headers=inference_headers,
                    json=_batch_call("credit", "credits-get"),
                )
            )
            try:
                await _wait_for_blocked_worker_and_heartbeat(started)
                assert len(worker_threads) == 1
                assert worker_threads[0] != loop_thread
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 200
            assert response.json()["result"]["isError"] is False

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_mcp_generation_lookup_runs_off_the_event_loop(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    raw_key = inference_headers["authorization"].removeprefix("Bearer ")
    context = mcp_routes.STORE.api_key_auth_context(raw_key)
    assert context is not None
    expected_workspace_id = context.api_key.workspace_id
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []

    class FakeGeneration:
        workspace_id = expected_workspace_id

        @staticmethod
        def to_openrouter_generation() -> dict[str, str]:
            return {"id": "gen-worker"}

    def blocking_generation(
        _store: InMemoryStore,
        generation_id: str,
    ) -> FakeGeneration:
        assert generation_id == "gen-worker"
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2.0), "MCP generation worker was never released"
        return FakeGeneration()

    monkeypatch.setattr(InMemoryStore, "get_generation", blocking_generation)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            request = asyncio.create_task(
                ac.post(
                    "/mcp",
                    headers=inference_headers,
                    json=_batch_call("generation", "generation-get", {"id": "gen-worker"}),
                )
            )
            try:
                await _wait_for_blocked_worker_and_heartbeat(started)
                assert len(worker_threads) == 1
                assert worker_threads[0] != loop_thread
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 200
            assert response.json()["result"]["isError"] is False

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_mcp_storage_batch_keeps_heartbeats_and_uses_workers(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    raw_key = inference_headers["authorization"].removeprefix("Bearer ")
    context = mcp_routes.STORE.api_key_auth_context(raw_key)
    assert context is not None
    expected_workspace_id = context.api_key.workspace_id
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []

    class FakeGeneration:
        workspace_id = expected_workspace_id

        @staticmethod
        def to_openrouter_generation() -> dict[str, str]:
            return {"id": "gen-batch"}

    def maybe_block() -> None:
        worker_threads.append(threading.get_ident())
        if len(worker_threads) == 1:
            started.set()
            assert release.wait(timeout=2.0), "MCP batch worker was never released"

    def batch_summary(_workspace_id: str) -> dict[str, int]:
        maybe_block()
        return {
            "total_credits": 10,
            "total_usage": 2,
            "reserved": 1,
            "available": 7,
        }

    def batch_generation(
        _store: InMemoryStore,
        _generation_id: str,
    ) -> FakeGeneration:
        maybe_block()
        return FakeGeneration()

    monkeypatch.setattr(mcp_routes, "live_credit_summary", batch_summary)
    monkeypatch.setattr(InMemoryStore, "get_generation", batch_generation)
    batch = [
        _batch_call("credit-1", "credits-get"),
        _batch_call("generation-1", "generation-get", {"id": "gen-batch"}),
        _batch_call("credit-2", "credits-get"),
        _batch_call("generation-2", "generation-get", {"id": "gen-batch"}),
    ]
    assert len(batch) == MAX_MCP_STORAGE_BATCH_ITEMS

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            request = asyncio.create_task(ac.post("/mcp", headers=inference_headers, json=batch))
            try:
                await _wait_for_blocked_worker_and_heartbeat(started)
                assert worker_threads[0] != loop_thread
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 200
            results = response.json()
            assert len(results) == MAX_MCP_STORAGE_BATCH_ITEMS
            assert all(item["result"]["isError"] is False for item in results)
            assert len(worker_threads) == MAX_MCP_STORAGE_BATCH_ITEMS
            assert all(worker_thread != loop_thread for worker_thread in worker_threads)

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_mcp_authentication_runs_off_the_event_loop(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    auth_thread: dict[str, int] = {}
    original_auth = InMemoryStore.api_key_auth_context

    def blocking_auth(self: InMemoryStore, raw: str):
        auth_thread["id"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=2.0), "MCP auth worker was never released"
        return original_auth(self, raw)

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", blocking_auth)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
            request = asyncio.create_task(
                async_client.post(
                    "/mcp",
                    headers=inference_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": "init",
                        "method": "initialize",
                        "params": {},
                    },
                )
            )
            try:
                for _ in range(200):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                assert started.is_set(), "MCP authentication never reached its worker"
                for _ in range(5):
                    await asyncio.sleep(0.005)
                assert auth_thread["id"] != loop_thread
            finally:
                release.set()
            response = await asyncio.wait_for(request, timeout=5.0)
            assert response.status_code == 200

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


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
        *[_batch_call(f"credit-{index}", "credits-get") for index in range(2)],
        *[
            _batch_call(f"generation-{index}", "generation-get", {"id": f"gen-{index}"})
            for index in range(2)
        ],
        *[
            _batch_call(f"unknown-{index}", f"unknown-tool-{index}")
            for index in range(MAX_MCP_BATCH_ITEMS - 5)
        ],
    ]
    assert len(batch) == MAX_MCP_BATCH_ITEMS

    response = client.post(
        "/mcp",
        headers={"authorization": "Bearer sk-tr-v1-invalid"},
        json=batch,
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid TrustedRouter API key"
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


def test_mcp_applies_authenticated_rate_limit_once_per_request(
    client: TestClient,
    inference_headers: dict[str, str],
    monkeypatch,
) -> None:
    authenticated_hits = 0
    original = InMemoryRateLimits.hit

    def limited(self, *, namespace: str, subject: str, limit: int, window_seconds: int):
        nonlocal authenticated_hits
        if namespace == "authenticated_api_key":
            authenticated_hits += 1
            return RateLimitHit(
                allowed=authenticated_hits == 1,
                limit=1,
                remaining=max(0, 1 - authenticated_hits),
                reset_at="2026-08-20T00:01:00Z",
                retry_after_seconds=60,
            )
        return original(
            self,
            namespace=namespace,
            subject=subject,
            limit=limit,
            window_seconds=window_seconds,
        )

    monkeypatch.setattr(InMemoryRateLimits, "hit", limited)
    batch = [
        _batch_call(str(index), "credits-get")
        for index in range(MAX_MCP_STORAGE_BATCH_ITEMS)
    ]

    first = client.post("/mcp", headers=inference_headers, json=batch)
    second = client.post("/mcp", headers=inference_headers, json=batch)

    assert first.status_code == 200
    assert all(item["result"]["isError"] is False for item in first.json())
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limited"
    assert authenticated_hits == 2


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
        *[
            _batch_call(str(index), "credits-get")
            for index in range(MAX_MCP_STORAGE_BATCH_ITEMS)
        ],
    ]

    response = client.post(
        "/mcp",
        headers={"authorization": "Bearer sk-tr-v1-unavailable"},
        json=batch,
    )

    assert response.status_code == 503
    assert auth_reads == 1
    assert response.json()["error"]["message"] == (
        "TrustedRouter API key authentication is unavailable"
    )
