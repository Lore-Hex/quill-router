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
