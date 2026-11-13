"""The featured section on /blog.

The index is chronological, which answers "what is newest" and not "what should
I read first". A reader landing on a 38-post archive cannot tell the posts that
explain what this company is from the ones reporting a benchmark result.

The assertion that matters most is that the section is not empty. A featured
section rendering zero cards looks exactly like a deliberate design choice, so
a typo in a slug would be invisible in review and in production.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.content.blog import (
    BLOG_POSTS_BY_SLUG,
    FEATURED_SLUGS,
    published_post,
    published_posts,
)
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
    assert len(posts) == len(published_posts())


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

    for post in published_posts():
        assert f'href="{post.href}"' in html, post.slug


def test_receipt_post_distinguishes_commitment_checks_from_chain_verification() -> None:
    body = BLOG_POSTS_BY_SLUG["every-response-can-prove-itself"].body_html

    assert (
        "commitment-checked before recording and fully chain-verified where supported"
        in body
    )
    assert "verified before it is recorded" not in body


COMBO = "combo-models-beat-the-best-model"


def test_a_post_dated_ahead_is_held_until_its_date() -> None:
    """Dating a post ahead embargoes it; the day arriving releases it."""
    assert BLOG_POSTS_BY_SLUG[COMBO].published_date == "2026-11-11"

    assert published_post(COMBO, today="2026-11-10") is None
    assert COMBO not in {post.slug for post in published_posts(today="2026-11-10")}

    assert published_post(COMBO, today="2026-11-11") is BLOG_POSTS_BY_SLUG[COMBO]
    assert COMBO in {post.slug for post in published_posts(today="2026-11-11")}
    assert len(published_posts(today="9999-12-31")) == len(BLOG_POSTS_BY_SLUG)


def test_an_embargoed_post_is_reachable_nowhere(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index, the sitemap and the post's own URL all have to agree, or the
    post leaks through whichever one was forgotten."""
    from trusted_router.content import blog

    def surfaces() -> tuple[int, str]:
        page = client.get(f"/blog/{COMBO}", headers={"accept": "text/html"})
        listed = "".join(
            client.get(path, headers={"accept": "text/html"}).text
            for path in ("/blog", "/sitemap-core.xml")
        )
        return page.status_code, listed

    monkeypatch.setattr(blog, "blog_today", lambda: "2026-11-10")
    status, listed = surfaces()
    assert status == 404
    assert COMBO not in listed

    monkeypatch.setattr(blog, "blog_today", lambda: "2026-11-11")
    status, listed = surfaces()
    assert status == 200
    assert listed.count(COMBO) >= 2


def test_combo_launch_opens_with_venturebeat_coverage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trusted_router.content import blog

    monkeypatch.setattr(blog, "blog_today", lambda: "2026-11-11")
    coverage_url = (
        "https://venturebeat.com/technology/"
        "trustedrouter-launches-three-new-encrypted-fusion-models-that-combine-"
        "multiple-llms-including-one-exclusively-with-u-s-model-providers"
    )
    opening = BLOG_POSTS_BY_SLUG[COMBO].body_html.strip().split("</p>", 1)[0]
    assert opening.startswith("<p><strong>In the news:</strong>")
    assert f'href="{coverage_url}"' in opening

    response = client.get(f"/blog/{COMBO}", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert f'href="{coverage_url}"' in response.text
    assert response.text.index(coverage_url) < response.text.index(
        "<p>Many models beat one model."
    )
