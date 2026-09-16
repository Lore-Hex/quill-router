from html.parser import HTMLParser
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from lightning_router.app import create_app


class Head(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.meta = {}
        self.links = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta":
            self.meta[attributes.get("name") or attributes.get("property")] = attributes.get("content")
        if tag == "link":
            self.links[attributes.get("rel")] = attributes.get("href")


@pytest.mark.parametrize("path", ["/", "/docs", "/pricing", "/terms", "/privacy", "/usage"])
def test_public_metadata_has_canonical_social_preview_and_correct_indexing(path, funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False),
                    base_url="https://www.lightningrouter.ai") as client:
        response = client.get(path, headers={"X-Forwarded-Host": "untrusted.invalid"})
        head = Head(response.text)
        assert head.links["canonical"] == "https://lightningrouter.ai" + path
        assert head.meta["og:url"] == head.links["canonical"]
        assert head.meta["og:image"] == "https://lightningrouter.ai/assets/lightningrouter-og.jpg"
        assert head.meta["og:image:width"] == "1732"
        assert head.meta["og:image:height"] == "908"
        assert head.meta["og:image:alt"]
        assert head.meta["twitter:card"] == "summary_large_image"
        assert head.meta["twitter:image"] == head.meta["og:image"]
        assert head.meta["twitter:site"] == "@lightningrouter"
        assert head.meta["description"] == head.meta["og:description"]
        assert head.meta["robots"] == ("noindex, nofollow" if path == "/usage" else "index, follow")
        assert "untrusted.invalid" not in response.text
        image = client.get("/assets/lightningrouter-og.jpg")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/jpeg"
        assert image.content.startswith(b"\xff\xd8\xff")
        assert len(image.content) < 500_000
    assert funding.lnd.creates == 0
    assert funding.credits.balances == {}


def test_sitemap_and_robots_exclude_private_workflows(funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        response = client.get("/sitemap.xml")
        assert response.status_code == 200
        root = ElementTree.fromstring(response.text)  # noqa: S314 - bundled, local sitemap fixture
        urls = {node.text for node in root.findall("{*}url/{*}loc")}
        assert urls == {"https://lightningrouter.ai" + path for path in ("/", "/docs", "/pricing", "/terms", "/privacy")}
        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        for rule in ("Sitemap: https://lightningrouter.ai/sitemap.xml", "Disallow: /usage", "Disallow: /api/", "Allow: /api/models"):
            assert rule in robots.text
    assert funding.lnd.creates == 0


def test_agent_guide_explains_safe_funding_and_separate_inference(funding, raw_key):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        for path in ("/llms.txt", "/docs.md"):
            response = client.get(path, headers={"Authorization": "Bearer " + raw_key})
            assert response.status_code == 200
            assert "https://api.trustedrouter.com/v1" in response.text
            assert "USD" in response.text
            assert "https://trust.trustedrouter.com" in response.text
            assert "https://lightningrouter.ai/api/models" in response.text
            assert raw_key not in response.text
            assert "no-store" in response.headers["cache-control"]
        home = client.get("/")
        assert 'href="/llms.txt"' in home.text
        assert 'href="/openapi.json"' in home.text
        assert "API activation is pending" not in home.text
        assert "<noscript>" in home.text
    assert funding.lnd.creates == 0
    assert funding.credits.balances == {}


@pytest.mark.parametrize("accept,markdown", [
    ("text/markdown", True), ("text/markdown; charset=utf-8", True),
    ("text/html;q=0.5, text/markdown;q=1", True), ("text/markdown, */*;q=0.8", True),
    ("text/html", False), ("*/*", False), ("", False),
    ("text/markdown;q=0, text/html", False), ("text/markdown;q=0.1, text/html", False),
    ("text/markdown;q=oops", False), ("text/markdown;q=2", False),
    ("text/markdown;q=NaN", False), ("text/markdown;q=0.2, text/*;q=0.9", False),
])
@pytest.mark.parametrize("path", ["/", "/docs", "/not-a-page"])
def test_markdown_negotiation_varies_and_respects_quality(path, accept, markdown, funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        response = client.get(path, headers={"Accept": accept})
    assert response.status_code == (404 if path == "/not-a-page" else 200)
    assert response.headers["content-type"].startswith("text/markdown" if markdown else "text/html")
    assert "accept" in response.headers["vary"].lower()
    if markdown:
        assert response.text.startswith("# ")
        assert "<html" not in response.text
        assert "https://lightningrouter.ai" in response.text


def test_404_is_recoverable_without_echoing_paths_or_changing_api_errors(funding, raw_key):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        for accept in ("text/html", "text/markdown"):
            response = client.get("/not-found/" + raw_key, headers={"Accept": accept})
            assert response.status_code == 404
            assert "/docs" in response.text and "/llms.txt" in response.text and "/sitemap.xml" in response.text
            assert response.headers["x-robots-tag"] == "noindex"
            assert raw_key not in response.text
        assert client.get("/api/no-such-endpoint").json() == {"detail": "Not Found"}
        assert client.get("/api/account").status_code == 401
        assert client.get("/api/usage").status_code == 401
        assert client.post("/openapi.json", content="{}").status_code == 405


def test_openapi_is_honest_read_only_discovery_not_payment_tools(funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert spec["openapi"] == "3.1.0"
        assert spec["servers"] == [{"url": "https://lightningrouter.ai"}]
        assert set(spec["paths"]) == {"/api/models", "/health"}
        for operation in spec["paths"].values():
            assert set(operation) == {"get"}
        assert spec["externalDocs"]["url"] == "https://trustedrouter.com/docs"
        assert "503" in spec["paths"]["/health"]["get"]["responses"]
    assert funding.lnd.creates == 0
    assert funding.credits.balances == {}
