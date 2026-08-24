from __future__ import annotations

import json
import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.routing_candidates import (
    auto_candidate_models,
    cheap_candidate_models,
    fast_candidate_models,
    free_candidate_models,
)

_HEADERS = {"x-trustedrouter-user": "segregation-owner@example.com"}


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )


@pytest.fixture
def segregated_models(client: TestClient) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for kind in ("machine", "agent", "human"):
        prompt_price = 100_000_000_000 if kind == "human" else 100
        response = client.post(
            "/v1/user-models",
            headers=_HEADERS,
            json={
                "name": f"Segregation {kind}",
                "slug": f"segregation-{kind}",
                "kind": kind,
                "description": f"A {kind} used to prove catalog segregation.",
                "display_name": f"segregation-{kind}-operator",
                "endpoint_url": "https://owner.example/v1",
                "prompt_price_microdollars_per_million_tokens": prompt_price,
                "completion_price_microdollars_per_million_tokens": prompt_price,
            },
        )
        assert response.status_code == 201, response.text
        models.append(response.json()["data"])
    return models


def _mcp_call(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "segregation",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_user_models_are_absent_from_every_frozen_catalog_surface(
    client: TestClient,
    segregated_models: list[dict[str, Any]],
    inference_headers: dict[str, str],
) -> None:
    model_ids = {model["id"] for model in segregated_models}
    bodies = {
        path: client.get(path).text
        for path in (
            "/v1/models",
            "/models",
            "/sitemap.xml",
            "/sitemap-core.xml",
            "/sitemap-models.xml",
            "/sitemap-providers.xml",
            "/sitemap-comparisons.xml",
            "/llms.txt",
            "/docs/llms.txt",
            "/docs/llms-full.txt",
            "/compare/models",
            "/choose/catalog.json",
        )
    }
    comparison = client.get(
        "/compare/models/trustedrouter/user-segregation-machine/"
        "vs/anthropic/claude-sonnet-4.6"
    )
    assert comparison.status_code == 404

    for model_id in model_ids:
        for surface, body in bodies.items():
            assert model_id not in body, surface
        listed = _mcp_call(
            client,
            inference_headers,
            "models-list",
            {"query": model_id, "limit": 100},
        )
        assert listed["isError"] is False
        listed_json = json.loads(listed["content"][0]["text"])
        assert listed_json["data"] == []
        fetched = _mcp_call(
            client,
            inference_headers,
            "model-get",
            {"model": model_id},
        )
        assert fetched["isError"] is True
        assert "Unknown model" in fetched["content"][0]["text"]

    candidate_ids = {
        model.id
        for models in (
            auto_candidate_models(),
            free_candidate_models(),
            cheap_candidate_models(),
            fast_candidate_models(),
        )
        for model in models
    }
    assert model_ids.isdisjoint(candidate_ids)


def test_user_models_exist_only_in_dedicated_api_and_direct_detail_pages(
    client: TestClient,
    segregated_models: list[dict[str, Any]],
) -> None:
    model_ids = {model["id"] for model in segregated_models}
    dedicated = client.get("/v1/models/user-provided")
    assert dedicated.status_code == 200, dedicated.text
    assert {model["id"] for model in dedicated.json()["data"]} == model_ids

    for model_id in model_ids:
        api_detail = client.get(f"/v1/models/user-provided/{model_id}")
        assert api_detail.status_code == 200, api_detail.text
        assert api_detail.json()["data"]["id"] == model_id
        html_detail = client.get(f"/models/{model_id}")
        assert html_detail.status_code == 200, html_detail.text
        assert "operated by a community member, not TrustedRouter" in html_detail.text
        assert 'meta name="robots" content="noindex"' in html_detail.text
