from __future__ import annotations

import json

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.content.blog import BLOG_POSTS_BY_SLUG, FEATURED_SLUGS

SLUG = "intelligence-gets-an-exchange"


def test_nyte_launch_post_and_discovery(client: TestClient) -> None:
    post = BLOG_POSTS_BY_SLUG[SLUG]
    assert post.published_date == "2026-10-15"
    assert SLUG in FEATURED_SLUGS
    response = client.get(post.href)
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one("h1").get_text() == post.title
    assert "Joseph Perla" in soup.select_one(".blog-meta").get_text()
    assert soup.select_one('link[rel="canonical"]')["href"].endswith(post.href)
    assert soup.select_one('meta[property="og:image"]')["content"].endswith(post.og_image)
    assert soup.select_one('a[href="mailto:enterprise@trustedrouter.com"]')
    graphs = [json.loads(node.string) for node in soup.select('script[type="application/ld+json"]')]
    assert any("BlogPosting" in json.dumps(graph) for graph in graphs)
    assert post.href in client.get("/blog").text
    assert post.href in client.get("/sitemap-core.xml").text


def test_nyte_launch_media_is_same_origin_and_playable(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(f"/blog/{SLUG}").text, "html.parser")
    video = soup.select_one("video")
    assert video.has_attr("controls")
    assert video.has_attr("playsinline")
    assert video["preload"] == "none"
    assert not video.has_attr("autoplay")
    assert video["src"].startswith("/static/blog/")
    for path in (video["poster"], BLOG_POSTS_BY_SLUG[SLUG].og_image):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/")
    response = client.get(video["src"], headers={"Range": "bytes=0-1023"})
    assert response.status_code == 206
    assert response.headers["content-type"].startswith("video/mp4")
    assert len(response.content) == 1024
