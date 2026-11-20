from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

PATHS = (
    "/claude-vs-chatgpt",
    "/gpt-vs-gemini",
    "/gemini-vs-claude",
    "/best-llm-for-coding",
    "/claude-code-router",
    "/opencode-models",
    "/coding-agent-api-keys",
)


@pytest.mark.parametrize("path", PATHS)
def test_comparison_guide_is_indexable_and_connected(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    html = response.text
    assert f'<link rel="canonical" href="https://trustedrouter.com{path}">' in html
    assert len(re.findall(r"<h1\b", html)) == 1
    assert 'content="noindex' not in html
    assert 'data-action="open-signin"' in html
    assert "Wallet-only signups" in html
    for related in PATHS:
        assert f'href="{related}"' in html
    graphs = re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    nodes = [node for graph in graphs for node in json.loads(graph).get("@graph", [])]
    assert any(node.get("@type") == "FAQPage" for node in nodes)


def test_guides_have_sitemap_entries_and_inbound_links(client: TestClient) -> None:
    sitemap = client.get("/sitemap-core.xml").text
    hub = client.get("/compare/models").text
    for path in PATHS:
        assert sitemap.count(f"<loc>https://trustedrouter.com{path}</loc>") == 1
        assert f'href="{path}"' in hub
    for path in ("/docs", "/docs/agent-setup"):
        html = client.get(path).text
        for related in PATHS[-3:]:
            assert f'href="{related}"' in html


def test_guide_internal_links_resolve(client: TestClient) -> None:
    # Check article and related-guide destinations, excluding global account controls.
    targets: set[str] = set()
    for path in PATHS:
        html = client.get(path).text
        article = html.split('<article class="content-document">', 1)[1].split("</article>", 1)[0]
        targets.update(urlsplit(link).path for link in re.findall(r'href="(/[^\"]*)"', article))
    for target in sorted(targets):
        assert client.get(target, follow_redirects=True).status_code == 200, target


def test_client_examples_keep_protocols_and_secrets_separate(client: TestClient) -> None:
    opencode = client.get("/opencode-models").text
    config_text = re.search(r'<pre><code>(\{.*?\})</code></pre>', opencode, re.S)
    assert config_text
    config = json.loads(config_text.group(1))
    provider = config["providers"]["trustedrouter"]
    assert provider["env"] == ["TRUSTEDROUTER_API_KEY"]
    assert provider["settings"]["baseURL"].endswith("/v1")
    claude = client.get("/claude-code-router").text
    assert 'ANTHROPIC_BASE_URL="https://api.trustedrouter.com"' in claude
    assert "does not support routing Claude Code to non-Claude models" in claude
    keys = client.get("/coding-agent-api-keys").text
    assert 'wire_api = "responses"' in keys
    assert 'env_key = "TRUSTEDROUTER_API_KEY"' in keys
    assert "Tab completion" in keys
