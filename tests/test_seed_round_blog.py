"""Contract tests for the seed-round announcement."""

from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.content.blog import BLOG_POSTS_BY_SLUG, FEATURED_SLUGS

SLUG = "we-raised-1-25m-seed"


def test_seed_round_post_is_featured_and_complete(client: TestClient) -> None:
    response = client.get(f"/blog/{SLUG}", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert SLUG in FEATURED_SLUGS
    assert "We Raised a $1.25M Seed" in response.text
    assert "billion-tokens-in-one-day milestone" in response.text
    assert "[link]" not in response.text


def test_seed_round_post_links_to_verifiable_product_evidence() -> None:
    body = BLOG_POSTS_BY_SLUG[SLUG].body_html

    expected_links = (
        "https://github.com/Lore-Hex/quill-router",
        "https://github.com/Lore-Hex/quill-cloud-proxy",
        "https://trust.trustedrouter.com/",
        "https://github.com/Lore-Hex/LLM-advisor",
        "/providers",
        "/leaderboard",
        "/pricing",
    )
    for href in expected_links:
        assert f'href="{href}"' in body


def test_seed_round_post_appears_in_featured_blog_section(client: TestClient) -> None:
    html = client.get("/blog", headers={"accept": "text/html"}).text
    featured = html[html.index("Featured") : html.index("All posts")]

    assert f'href="/blog/{SLUG}"' in featured
