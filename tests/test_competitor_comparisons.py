from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from trusted_router.competitor_comparisons import COMPETITOR_COMPARISONS


def _json_ld(response_text: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        response_text,
        flags=re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_comparison_registry_is_complete_and_evidence_backed() -> None:
    slugs = [comparison.slug for comparison in COMPETITOR_COMPARISONS]

    assert len(slugs) >= 17
    assert len(slugs) == len(set(slugs))
    assert len({comparison.summary for comparison in COMPETITOR_COMPARISONS}) == len(slugs)
    assert len({comparison.migration for comparison in COMPETITOR_COMPARISONS}) == len(slugs)
    for comparison in COMPETITOR_COMPARISONS:
        assert len(comparison.rows) == 8
        assert len(comparison.sources) >= 2
        assert len(comparison.faq_items) == 3
        assert all(source.url.startswith("https://") for source in comparison.sources)
        assert all(source.label.strip() for source in comparison.sources)
        assert all(all(cell.strip() for cell in row) for row in comparison.rows)


def test_gateway_comparison_directory_links_every_page(client: TestClient) -> None:
    response = client.get("/compare")

    assert response.status_code == 200
    assert '<link rel="canonical" href="https://trustedrouter.com/compare">' in response.text
    assert "Choose by architecture, not by logo." in response.text
    for comparison in COMPETITOR_COMPARISONS:
        assert f'href="{comparison.href}"' in response.text
        assert comparison.name in response.text

    graph = _json_ld(response.text)
    nodes = graph["@graph"]
    assert isinstance(nodes, list)
    item_list = next(node for node in nodes if node["@type"] == "ItemList")
    assert item_list["numberOfItems"] == len(COMPETITOR_COMPARISONS)


def test_each_gateway_comparison_has_unique_evidence_and_schema(client: TestClient) -> None:
    for comparison in COMPETITOR_COMPARISONS:
        response = client.get(comparison.href)

        assert response.status_code == 200, comparison.slug
        assert (
            f'<link rel="canonical" href="https://trustedrouter.com{comparison.href}">'
            in response.text
        )
        assert comparison.name in response.text
        assert "Sources checked August 16, 2026" in response.text
        assert 'href="/compare"' in response.text
        assert 'href="/benchmarks/reports"' in response.text
        assert "https://trust.trustedrouter.com" in response.text
        for source in comparison.sources:
            assert f'href="{source.url}"' in response.text

        graph = _json_ld(response.text)
        nodes = graph["@graph"]
        assert isinstance(nodes, list)
        assert any(node["@type"] == "FAQPage" for node in nodes)
        page = next(node for node in nodes if node["@type"] == "WebPage")
        assert page["dateModified"] == "2026-08-16"


def test_unknown_gateway_comparison_returns_real_404(client: TestClient) -> None:
    response = client.get("/compare/not-a-real-router")

    assert response.status_code == 404
    assert "Page not found" in response.text


def test_competitor_route_does_not_shadow_model_comparisons(client: TestClient) -> None:
    index = client.get("/compare/models")
    detail = client.get("/compare/models/moonshotai/kimi-k2.6/vs/z-ai/glm-5.2")

    assert index.status_code == 200
    assert detail.status_code == 200
    assert "Compare AI models" in index.text
    assert "Kimi K2.6" in detail.text
    assert "GLM 5.2" in detail.text


def test_core_sitemap_contains_every_gateway_comparison_once(client: TestClient) -> None:
    response = client.get("/sitemap-core.xml")

    assert response.status_code == 200
    assert response.text.count("<loc>https://trustedrouter.com/compare</loc>") == 1
    for comparison in COMPETITOR_COMPARISONS:
        url = f"<loc>https://trustedrouter.com{comparison.href}</loc>"
        assert response.text.count(url) == 1, comparison.slug


# ---------------------------------------------------------------------------
# Voice and claims guards. The first generation of these pages shipped with a
# canned FAQ answer pasted seventeen times, "open source" applied to BUSL-1.1
# repos, and two claims production could not support ("every OpenRouter model
# id resolves", "30-day burn rates"). These tests keep each of those from
# coming back, and hold the articles to the house style guide's hard bans.
# ---------------------------------------------------------------------------

_BANNED_WORDS = (
    "quietly",
    "seamlessly",
    "effortlessly",
    "delve",
    "tapestry",
    "moreover",
    "furthermore",
    "boasts",
    "ever-evolving",
    "in today's landscape",
    "navigate the complexities",
    "at the end of the day",
)

_BANNED_CLAIMS = (
    # BUSL-1.1 is source-available; "open source" may describe competitors,
    # never the TrustedRouter gateway or control plane.
    "open-source control plane",
    "open source control plane",
    "hosted open-source",
    # Measured 2026-08-16: 168 of 413 OpenRouter ids resolve.
    "every openrouter model id",
    # The status page publishes 5m-24h burn windows; no 30-day burn rate exists.
    "30-day burn rates",
    # No combined route ids exist, only composable request preferences.
    "combo routes",
)


def _entry_text(comparison) -> str:
    parts = [
        comparison.summary,
        comparison.competitor_fit,
        comparison.trustedrouter_fit,
        comparison.migration,
        comparison.article_html,
    ]
    parts.extend(cell for row in comparison.rows for cell in row)
    parts.extend(text for pair in comparison.faq_items for text in pair)
    return " ".join(parts).lower()


def test_no_banned_words_or_unsupported_claims() -> None:
    for comparison in COMPETITOR_COMPARISONS:
        text = _entry_text(comparison)
        for word in _BANNED_WORDS:
            assert word not in text, f"{comparison.slug}: banned word {word!r}"
        for claim in _BANNED_CLAIMS:
            assert claim not in text, f"{comparison.slug}: unsupported claim {claim!r}"


def test_every_page_has_a_substantial_unique_article() -> None:
    articles = [comparison.article_html for comparison in COMPETITOR_COMPARISONS]
    assert all(len(article.split()) >= 400 for article in articles)
    assert len(set(articles)) == len(articles)


def test_faq_answers_are_written_per_competitor_not_canned() -> None:
    """The regression this guards: one templated TrustedRouter answer pasted
    into every page, which reads as exactly what it is."""
    second_answers = {comparison.faq_items[1][1] for comparison in COMPETITOR_COMPARISONS}
    assert len(second_answers) == len(COMPETITOR_COMPARISONS)


def test_each_page_cites_at_least_four_specific_sources() -> None:
    for comparison in COMPETITOR_COMPARISONS:
        assert len(comparison.sources) >= 4, comparison.slug


def test_article_links_are_https_or_site_relative() -> None:
    for comparison in COMPETITOR_COMPARISONS:
        for href in re.findall(r'href="([^"]+)"', comparison.article_html):
            assert href.startswith(("https://", "/")), f"{comparison.slug}: {href}"


def test_shared_matrix_column_says_source_available() -> None:
    for comparison in COMPETITOR_COMPARISONS:
        deployment_row = comparison.rows[0]
        assert "source-available (BUSL-1.1)" in deployment_row[2]
