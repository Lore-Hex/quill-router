from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.pricing import PriceTier
from trusted_router.routes import mcp_advisor as advisor
from trusted_router.storage import InMemoryStore


def rpc(client: TestClient, method: str, params=None, **kwargs):
    return client.post(
        "/mcp/advisor",
        json={"jsonrpc": "2.0", "id": 0, "method": method, "params": params or {}},
        **kwargs,
    )


def call(client: TestClient, name: str, arguments=None):
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200
    return response.json()["result"]


def test_advisor_public_handshake_and_annotations(client, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("public advisor attempted credential storage")

    monkeypatch.setattr(InMemoryStore, "api_key_auth_context", forbidden)
    init = rpc(client, "initialize").json()
    assert init["id"] == 0
    assert init["result"]["serverInfo"]["name"] == "trustedrouter-model-advisor"
    assert rpc(client, "ping").json()["result"] == {}
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == set(advisor.TOOLS)
    for tool in tools:
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }
        assert tool["securitySchemes"] == [{"type": "noauth"}]
        assert tool["inputSchema"]["additionalProperties"] is False
    assert (
        client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).status_code
        == 401
    )


@pytest.mark.parametrize(
    "name", ["chat-send", "credits-get", "generation-get", "fetch", "delete-account"]
)
def test_advisor_never_dispatches_private_or_billable_tools(client, name):
    result = call(client, name)
    assert result["isError"] is True
    assert "Unknown read-only" in result["content"][0]["text"]


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("search_models", {"limit": 21}),
        ("search_models", {"limit": True}),
        ("search_models", {"query": "x" * 121}),
        ("search_models", {"min_context": "1000"}),
        ("search_models", {"privacy": "e2ee"}),
        ("search_models", {"prompt": "private-content-sentinel"}),
        ("compare_models", {"models": []}),
        ("compare_models", {"models": ["a/b"] * 6}),
        (
            "estimate_cost",
            {"model": "a/b", "provider": "a", "input_tokens": -1, "output_tokens": 3},
        ),
        ("get_provider", {"provider": "https://169.254.169.254"}),
        ("search_docs", {"query": ""}),
    ],
)
def test_advisor_rejects_invalid_args_without_echo(client, name, arguments):
    result = call(client, name, arguments)
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Invalid tool arguments; follow the tool schema"


@pytest.fixture
def catalog(monkeypatch):
    model = Model(id="test/model", name="Test", provider="near-ai", context_length=1_000_000)
    endpoint = ModelEndpoint(
        id="test-near",
        model_id=model.id,
        provider="near-ai",
        usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=1_000_000,
        completion_price_microdollars_per_million_tokens=2_000_000,
        price_tiers=(
            PriceTier(200_000, 1_000_000, 2_000_000),
            PriceTier(None, 3_000_000, 6_000_000),
        ),
    )
    monkeypatch.setattr(advisor, "MODELS", {model.id: model})
    monkeypatch.setattr(
        advisor,
        "_shapes",
        lambda: {model.id: {"id": model.id, "name": "Test", "context_length": 1_000_000}},
    )
    monkeypatch.setattr(
        advisor,
        "endpoints_for_model",
        lambda _: [endpoint, replace(endpoint, id="byok", usage_type="BYOK")],
    )
    return model, endpoint


def test_search_limits_context_and_privacy(client, catalog, monkeypatch):
    monkeypatch.setattr(advisor, "endpoint_zero_data_retention", lambda _: False)
    assert call(client, "search_models", {"privacy": "zdr"})["structuredContent"]["models"] == []
    assert (
        call(client, "search_models", {"query": "TEST"})["structuredContent"]["total_matches"] == 1
    )
    assert (
        call(client, "search_models", {"min_context": 1_000_001})["structuredContent"]["models"]
        == []
    )


@pytest.mark.parametrize(
    "zdr,tee,e2ee,expected",
    [
        (True, True, True, 1),
        (False, True, True, 0),
        (None, True, True, 0),
        (True, True, False, 0),
        (True, False, True, 0),
    ],
)
def test_confidential_requires_all_three_on_same_prepaid_route(
    client, catalog, monkeypatch, zdr, tee, e2ee, expected
):
    monkeypatch.setattr(advisor, "endpoint_zero_data_retention", lambda _: zdr)
    monkeypatch.setattr(advisor, "endpoint_confidential_compute", lambda _: tee)
    monkeypatch.setattr(advisor, "endpoint_e2ee", lambda _: e2ee)
    result = call(client, "compare_models", {"models": ["test/model"], "privacy": "confidential"})
    assert result["isError"] is False
    assert len(result["structuredContent"]["models"][0]["routes"]) == expected


def test_compare_deduplicates_and_contains_evidence_links(client, catalog):
    result = call(client, "compare_models", {"models": ["test/model", "test/model"]})[
        "structuredContent"
    ]
    assert len(result["models"]) == 1
    route = result["models"][0]["routes"][0]
    assert route["usage_type"] == "Credits"
    assert route["privacy_policy_url"].startswith("https://")
    assert "inference_location" in route
    assert "pricing_usd_per_token" in route


@pytest.mark.parametrize(
    "input_tokens,output_tokens,requests,expected",
    [(1_000, 500, 100, "0.2"), (300_000, 100_000, 1, "1.5"), (0, 0, 1, "0")],
)
def test_estimate_uses_retail_rates_context_tiers_and_counts(
    client, catalog, input_tokens, output_tokens, requests, expected
):
    result = call(
        client,
        "estimate_cost",
        {
            "model": "test/model",
            "provider": "near-ai",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "requests": requests,
        },
    )
    assert result["isError"] is False
    data = result["structuredContent"]
    assert Decimal(data["estimated_total_token_cost"]) == Decimal(expected)
    assert "not a quote" in data["assumptions"]


@pytest.mark.parametrize(
    "model,provider,inputs,outputs",
    [
        ("missing/model", "near-ai", 1, 1),
        ("test/model", "missing", 1, 1),
        ("test/model", "near-ai", 1_000_000, 1),
    ],
)
def test_estimate_rejects_unknown_route_and_context_overflow(
    client, catalog, model, provider, inputs, outputs
):
    assert call(
        client,
        "estimate_cost",
        {"model": model, "provider": provider, "input_tokens": inputs, "output_tokens": outputs},
    )["isError"]


def test_advisor_provider_and_documentation(client, monkeypatch):
    monkeypatch.setattr(
        advisor,
        "docs_llms_full_txt",
        lambda _: "# Routing\n\nUse provider.only to pin a route.\n\nAnother topic",
    )
    assert call(client, "get_provider", {"provider": "near-ai"})["structuredContent"][
        "source"
    ].endswith("/near-ai")
    assert call(client, "get_provider", {"provider": "unknown-provider"})["isError"]
    docs = call(client, "search_docs", {"query": "provider.only"})["structuredContent"]
    assert docs["excerpts"] == ["Use provider.only to pin a route."]


def test_public_protocol_and_body_limits(client):
    assert (
        client.post("/mcp/advisor", content="x" * (advisor.MAX_BODY_BYTES + 1)).status_code == 413
    )
    assert client.post("/mcp/advisor", content="{").status_code == 400
    assert client.post("/mcp/advisor", json=[]).status_code == 400
    assert (
        client.post(
            "/mcp/advisor", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        ).status_code
        == 202
    )
    assert rpc(client, "unsupported").json()["error"]["code"] == -32601
    assert rpc(client, "tools/list", headers={"Origin": "https://evil.example"}).status_code == 403
    assert rpc(client, "tools/list", headers={"Origin": "https://chatgpt.com"}).status_code == 200
    invalid = client.post("/mcp/advisor", json={"jsonrpc": "2.0", "id": {}, "method": "tools/list"})
    assert invalid.json()["error"]["code"] == -32600


def test_unexpected_error_does_not_expose_details(client, monkeypatch):
    def broken():
        raise RuntimeError("private-content-sentinel")

    monkeypatch.setattr(advisor, "_shapes", broken)
    result = call(client, "search_models")
    assert result["isError"]
    assert "private-content-sentinel" not in str(result)


def test_advisor_is_on_public_surface_not_account_surface():
    for surface, expected in (("public", 200), ("control", 404)):
        app = create_app(
            Settings(environment="test", storage_backend="memory", service_surface=surface)
        )
        with TestClient(app) as client:
            assert rpc(client, "initialize").status_code == expected
            challenge = client.get("/.well-known/openai-apps-challenge")
            assert challenge.status_code == expected
            if expected == 200:
                assert challenge.headers["content-type"].startswith("text/plain")
                assert challenge.text == "qYEFG9OeQ9eQ42p_rin3l4iW92CkpERa_z6blaw39_U"
