"""The featured section on /blog.

The index is chronological, which answers "what is newest" and not "what should
I read first". A reader landing on a 38-post archive cannot tell the posts that
explain what this company is from the ones reporting a benchmark result.

The assertion that matters most is that the section is not empty. A featured
section rendering zero cards looks exactly like a deliberate design choice, so
a typo in a slug would be invisible in review and in production.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.content.blog import BLOG_POSTS_BY_SLUG, FEATURED_SLUGS
from trusted_router.dashboard import _blog_index_posts, _featured_blog_posts


def test_featured_slugs_all_resolve() -> None:
    """A slug that matches no post is skipped at render time so the page cannot
    500 over an editorial typo. This is where that typo is supposed to surface
    instead."""
    missing = [slug for slug in FEATURED_SLUGS if slug not in BLOG_POSTS_BY_SLUG]

    assert not missing, f"featured slugs match no post: {missing}"


def test_the_featured_section_is_not_empty() -> None:
    featured = _featured_blog_posts(Settings(environment="test"))

    assert len(featured) == len(FEATURED_SLUGS)
    assert all(item.image for item in featured)


def test_featured_is_newest_first_regardless_of_slug_order() -> None:
    """FEATURED_SLUGS chooses WHICH posts are featured, not their order.

    Both sections on the page read newest-first so a reader does not have to
    work out that one follows a different rule. The assertion is meaningful
    only because the slug order and the date order currently DISAGREE -- if
    they ever coincide this test would pass without exercising the sort, so it
    checks that too.
    """
    featured = _featured_blog_posts(Settings(environment="test"))
    dates = [item.post.published_date for item in featured]

    assert dates == sorted(dates, reverse=True)
    slug_order = [BLOG_POSTS_BY_SLUG[s].published_date for s in FEATURED_SLUGS]
    assert slug_order != dates, (
        "FEATURED_SLUGS order now coincides with newest-first, so this test no "
        "longer proves the sort runs; reorder the tuple or pick another case"
    )


def test_the_archive_is_always_newest_first() -> None:
    """BLOG_POSTS is hand-maintained and had drifted: the-models-that-say-no
    (2026-06-16) sat above a 2026-06-17 post. The order is sorted at render
    time now, so this asserts the rendered list rather than the tuple."""
    posts = _blog_index_posts(Settings(environment="test"))
    dates = [item.post.published_date for item in posts]

    assert dates == sorted(dates, reverse=True)
    assert len(posts) == len(BLOG_POSTS_BY_SLUG)


def test_the_blog_index_shows_featured_above_the_archive(client: TestClient) -> None:
    html = client.get("/blog", headers={"accept": "text/html"}).text

    assert "Featured" in html
    assert "All posts" in html
    assert html.index("Featured") < html.index("All posts")
    for slug in FEATURED_SLUGS:
        assert f'href="/blog/{slug}"' in html, slug


def test_featured_posts_still_appear_in_the_archive(client: TestClient) -> None:
    """Featuring a post promotes it; it does not remove it from the record. A
    reader scanning by date should still find it where its date says it is."""
    html = client.get("/blog", headers={"accept": "text/html"})
    archive = html.text[html.text.index("All posts") :]

    for slug in FEATURED_SLUGS:
        assert f'href="/blog/{slug}"' in archive, slug


def test_every_post_is_still_listed(client: TestClient) -> None:
    """The archive is the complete record; the featured section must not have
    quietly filtered it."""
    html = client.get("/blog", headers={"accept": "text/html"}).text

    for slug in BLOG_POSTS_BY_SLUG:
        assert f'href="/blog/{slug}"' in html, slug
