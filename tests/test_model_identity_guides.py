from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

PATHS = (
    "/verify-ai-model",
    "/llm-model-substitution",
    "/confidential-inference-verification",
)


@pytest.mark.parametrize("path", PATHS)
def test_identity_guides_have_canonicals_faqs_and_evidence(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    html = response.text
    assert f'<link rel="canonical" href="https://trustedrouter.com{path}">' in html
    assert len(re.findall(r"<h1\b", html)) == 1
    assert "runtime_verified: false" in html
    assert "November 19, 2026" in html
    assert "up to 1 million free tokens" in html
    assert "Wallet-only signups" in html
    assert 'data-action="open-signin"' in html
    for related in (*PATHS, "/model-precision", "/docs/provider-routing", "/docs/receipts"):
        assert f'href="{related}"' in html
    graphs = re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    assert any(node.get("@type") == "FAQPage" for graph in graphs for node in json.loads(graph).get("@graph", []))
    assert "https://arxiv.org/" in html or "https://docs.nvidia.com/attestation/" in html


def test_identity_guides_are_discoverable_from_sitemap_and_existing_pages(client: TestClient) -> None:
    sitemap = client.get("/sitemap-core.xml")
    assert sitemap.status_code == 200
    for path in PATHS:
        assert sitemap.text.count(f"<loc>https://trustedrouter.com{path}</loc>") == 1
    for path in ("/docs", "/docs/provider-routing", "/docs/receipts", "/model-precision"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'href="/verify-ai-model"' in response.text


def test_precision_page_retains_honest_metadata_scope(client: TestClient) -> None:
    html = client.get("/model-precision").text
    assert "runtime_verified" in html
    assert "501" in html
    assert "Why does the same AI model give different results across providers?" in html
    assert 'href="https://arxiv.org/abs/2410.20247"' in html
