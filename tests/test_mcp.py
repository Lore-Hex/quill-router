from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient


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


def test_mcp_models_list_includes_sonnet_5_and_subagent(client: TestClient) -> None:
    sonnet_payload = _mcp_call(client, "models-list", {"query": "sonnet-5", "limit": 5})
    sonnet = _tool_json(sonnet_payload)
    assert [item["id"] for item in sonnet["data"]] == ["anthropic/claude-sonnet-5"]

    subagent_payload = _mcp_call(client, "model-get", {"model": "trustedrouter/subagent"})
    subagent = _tool_json(subagent_payload)["data"]
    assert subagent["id"] == "trustedrouter/subagent"
    assert subagent["trustedrouter"]["route_kind"] == "subagent_orchestration"
    assert subagent["trustedrouter"]["byok_available"] is False


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
