from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

PATHS = (
    "/compare/aws-bedrock",
    "/compare/google-vertex-ai",
    "/compare/azure-ai-foundry",
    "/compare/bedrock-vs-vertex-ai-vs-azure-ai-foundry",
    "/multi-cloud-llm-api",
    "/bedrock-throttling-quota-guide",
)


@pytest.mark.parametrize("path", PATHS)
def test_cloud_guides_are_canonical_linked_and_have_faq_schema(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    html = response.text
    assert f'<link rel="canonical" href="https://trustedrouter.com{path}">' in html
    assert len(re.findall(r"<h1\b", html)) == 1
    assert 'id="cloud-market-title"' in html
    assert "Try the OpenAI-compatible endpoint" in html
    for related in PATHS:
        assert f'href="{related}"' in html
    graphs = re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    assert any(node.get("@type") == "FAQPage" for graph in graphs for node in json.loads(graph).get("@graph", []))
    assert "Sources reviewed November 18, 2026" in html or "Sources checked November 18, 2026" in html


def test_cloud_guide_sitemap_and_directory_discovery(client: TestClient) -> None:
    sitemap = client.get("/sitemap-core.xml")
    assert sitemap.status_code == 200
    for path in PATHS:
        assert sitemap.text.count(f"<loc>https://trustedrouter.com{path}</loc>") == 1
    directory = client.get("/compare").text
    for path in PATHS:
        assert f'href="{path}"' in directory
