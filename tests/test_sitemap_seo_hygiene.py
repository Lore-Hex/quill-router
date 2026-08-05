from __future__ import annotations

from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from defusedxml import ElementTree
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.seo_meta import seo_meta_description, seo_title


class _SeoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_head = False
        self.in_title = False
        self.title_parts: list[str] = []
        self.canonical: str | None = None
        self.description: str | None = None
        self.robots: str | None = None
        self.images_without_alt = 0
        self.links: set[str] = set()

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "head":
            self.in_head = True
        elif tag == "title" and self.in_head and not self.title_parts:
            self.in_title = True
        elif tag == "link" and self.in_head and values.get("rel") == "canonical":
            self.canonical = values.get("href")
        elif tag == "meta" and self.in_head:
            name = (values.get("name") or "").lower()
            if name == "description":
                self.description = values.get("content")
            elif name == "robots":
                self.robots = values.get("content")
        elif tag == "img" and not (values.get("alt") or "").strip():
            self.images_without_alt += 1
        elif tag == "a" and values.get("href"):
            self.links.add(values["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.in_title:
            self.in_title = False
        elif tag == "head":
            self.in_head = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def _sitemap_urls(client: TestClient) -> list[str]:
    namespace = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    child_paths = (
        "/sitemap-core.xml",
        "/sitemap-providers.xml",
        "/sitemap-models.xml",
        "/sitemap-comparisons.xml",
    )
    urls: list[str] = []
    for child_path in child_paths:
        response = client.get(child_path)
        assert response.status_code == 200
        root = ElementTree.fromstring(response.content)
        urls.extend(
            node.text for node in root.findall("sitemap:url/sitemap:loc", namespace) if node.text
        )
    return urls


def test_every_sitemap_page_has_clean_search_metadata(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"rate_limit_enabled": False})
    client = TestClient(create_app(settings, init_observability=False))
    urls = _sitemap_urls(client)
    assert len(urls) >= 3_500
    assert len(urls) == len(set(urls))
    sitemap_paths = {urlsplit(url).path for url in urls}

    issues: list[str] = []
    link_sources: dict[str, set[str]] = defaultdict(set)
    for url in urls:
        split = urlsplit(url)
        path = split.path + (f"?{split.query}" if split.query else "")
        response = client.get(path, follow_redirects=False)
        if response.status_code != 200:
            issues.append(f"{path}: status {response.status_code}")
            continue
        if "text/html" not in response.headers.get("content-type", ""):
            continue

        parser = _SeoParser()
        parser.feed(response.text)
        if parser.robots and "noindex" in parser.robots.lower():
            issues.append(f"{path}: noindex page is listed in sitemap")
        if parser.canonical != url:
            issues.append(f"{path}: canonical {parser.canonical!r} != {url!r}")
        if not parser.title or len(parser.title) > 60:
            issues.append(f"{path}: title length {len(parser.title)}")
        description_length = len(parser.description or "")
        if not 120 <= description_length <= 160:
            issues.append(f"{path}: description length {description_length}")
        if parser.images_without_alt:
            issues.append(f"{path}: {parser.images_without_alt} images have empty alt text")

        for href in parser.links:
            target = urlsplit(urljoin(url, href))
            if target.hostname == settings.trusted_domain and not target.fragment:
                target_path = target.path + (f"?{target.query}" if target.query else "")
                link_sources[target_path].add(path)

    linked_redirects: list[str] = []
    for target_path, sources in sorted(link_sources.items()):
        if target_path in sitemap_paths:
            continue
        response = client.get(target_path, follow_redirects=False)
        if 300 <= response.status_code < 400:
            linked_redirects.append(
                f"{target_path}: {response.status_code} -> "
                f"{response.headers.get('location')!r}, linked from {sorted(sources)[:3]}"
            )

    assert not issues, "\n".join(issues[:100])
    assert not linked_redirects, "\n".join(linked_redirects[:100])


def test_seo_metadata_helpers_bound_generated_text() -> None:
    assert seo_title("Models | TrustedRouter") == "Models | TrustedRouter"
    assert seo_title("Security") == "Security | TrustedRouter"
    assert seo_title("A" * 80).endswith("...")
    assert len(seo_title("A" * 80)) == 60
    assert seo_meta_description("Repeated words " * 20).endswith("...")
    assert len(seo_meta_description("Repeated words " * 20)) <= 160
