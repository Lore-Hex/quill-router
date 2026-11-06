from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_abliterated_deepseek_is_public(client: TestClient, method: str) -> None:
    response = client.request(method, "/abliterated-deepseek")
    assert response.status_code == 200


def test_abliterated_deepseek_has_evidence_and_explicit_route(client: TestClient) -> None:
    response = client.get("/abliterated-deepseek")
    body = response.text
    assert '<link rel="canonical" href="https://trustedrouter.com/abliterated-deepseek">' in body
    assert "dealignai/deepseek-v4.1-flash-1017-uncensored" in body
    assert "DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8" in body
    assert "cyberdefense" in body
    assert "Alert triage" in body
    assert "human approval" in body
    assert '"allow_fallbacks": false' in body
    assert '"min_privacy": "confidential"' in body
    assert "Images, tools, structured-output schemas, and BYOK" in body
    assert "not an official DeepSeek API release" in body
    assert "not a guarantee of accuracy" in body
    scripts = re.findall(r'<script\b[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', body, re.S)
    assert scripts
    for script in scripts:
        json.loads(script)
    sitemap = client.get("/sitemap-core.xml").text
    assert sitemap.count("<loc>https://trustedrouter.com/abliterated-deepseek</loc>") == 1


@pytest.mark.parametrize("path", [
    "/resources", "/best-llm-router", "/deepseek-api-privacy",
    "/providers/maple", "/providers/abliteration", "/providers/abliterate",
])
def test_abliterated_guide_has_discovery_links(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert 'href="/abliterated-deepseek"' in response.text


def test_existing_opportunity_pages_have_decision_guides(client: TestClient) -> None:
    router = client.get("/best-llm-router")
    assert "Choose your AI router by the decision that matters" in router.text
    us = client.get("/us-ai-models")
    assert 'href="#us-open-weights"' in us.text
    assert 'id="us-open-weights"' in us.text
    assert "not a data-residency guarantee" in us.text
