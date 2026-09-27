from __future__ import annotations

from html.parser import HTMLParser

import pytest
from defusedxml import ElementTree
from fastapi.testclient import TestClient

FEATURES = (
    ("/performance-routing", "/docs/performance-routing", "preferred_max_latency"),
    ("/spend-controls", "/docs/spend-controls", "budget_strict"),
    ("/prompt-caching", "/docs/prompt-caching", "session_id"),
    ("/model-precision", "/docs/model-precision", "runtime_verified"),
)


class PageMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.canonical = ""
        self.h1_count = 0
        self.links: set[str] = set()
        self.robots = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "link" and values.get("rel") == "canonical":
            self.canonical = values.get("href") or ""
        if tag == "meta" and values.get("name") == "robots":
            self.robots = values.get("content") or ""
        if tag == "h1":
            self.h1_count += 1
        if tag == "a" and values.get("href"):
            self.links.add(values["href"] or "")


@pytest.mark.parametrize("landing,guide,field", FEATURES)
def test_feature_pages_have_distinct_indexable_content_and_links(
    client: TestClient, landing: str, guide: str, field: str,
) -> None:
    for path, counterpart in ((landing, guide), (guide, landing)):
        response = client.get(path)
        assert response.status_code == 200
        parser = PageMetadata()
        parser.feed(response.text)
        assert parser.canonical == "https://trustedrouter.com" + path
        assert parser.h1_count == 1
        assert "noindex" not in parser.robots
        assert counterpart in parser.links
        assert field in response.text
        assert 'property="og:image"' in response.text
        assert 'type="application/ld+json"' in response.text
        assert client.head(path).status_code == 200
    assert client.get(landing).text != client.get(guide).text


def test_feature_pages_are_in_sitemap_and_linked_for_people_and_agents(client: TestClient) -> None:
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ElementTree.fromstring(client.get("/sitemap-core.xml").content)
    urls = [node.text for node in root.findall("s:url/s:loc", ns)]
    assert len(urls) == len(set(urls))
    resources = client.get("/resources").text
    docs = client.get("/docs").text
    for landing, guide, _ in FEATURES:
        for path in (landing, guide):
            assert "https://trustedrouter.com" + path in urls
        assert f'href="{landing}"' in resources
        assert f'href="{guide}"' in docs
        for index in ("/llms.txt", "/docs/llms.txt", "/docs/llms-full.txt"):
            assert guide in client.get(index).text


def test_feature_docs_keep_guarantees_scoped(client: TestClient) -> None:
    budgets = client.get("/docs/spend-controls").text
    assert "off by default" in budgets
    assert "much slower" in budgets
    assert "estimated" in budgets
    assert "management key" in budgets
    assert '"https://trustedrouter.com/v1/keys"' in budgets
    assert "429" in budgets and "503" in budgets
    precision = client.get("/docs/model-precision").text
    assert "published_serving_config" in precision
    assert "not per-request proof" in precision
    assert "501" in precision
    assert "null" in precision
    assert 'f"https://trustedrouter.com/v1/models/{model}/endpoints"' in precision
    performance = client.get("/docs/performance-routing").text
    assert "p50" in performance and "p99" in performance
    assert "not a timeout" in performance
    assert "provider.order" in performance
    assert "400" in performance
    cache = client.get("/docs/prompt-caching").text
    assert 'id="session-affinity"' in cache
    assert '"session_id":' in cache
    assert "best-effort" in cache and "process-local" in cache
