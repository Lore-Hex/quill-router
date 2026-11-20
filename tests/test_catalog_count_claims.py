"""Marketing totals are stable; competitor facts and per-route evidence stay exact."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.content.blog import BLOG_POSTS, BLOG_POSTS_BY_SLUG, BlogPost
from trusted_router.dashboard import docs_llms_full_txt, docs_llms_txt, llms_txt

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/trusted_router/templates"
PICKER = ROOT / "src/trusted_router/static/choose-app.js"
STATIC = ROOT / "src/trusted_router/static"
CLAIM = re.compile(
    r"\b(?:(?:more than |over )?\d[\d,]*\+?|hundreds of)\s+"
    r"(?:(?:additional|other|more|public|AI|LLM)\s+)?"
    r"(?:model\s+(?:ids|routes)|models|routes|providers)\b", re.IGNORECASE,
)
# Exact competitor facts and a synthesis panel limit, not TrustedRouter totals.
# Keep exemptions phrase-sized: never exempt an entire compare page or row.
EXEMPT = (
    "400+ model ids across 100+ providers",
    "327 models as of August 2026",
    "1 to 8 models",
    "1-8 model IDs",
)


def invalid_claims(text: str) -> list[str]:
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"served by \d+ providers\b", "", text)
    for phrase in EXEMPT:
        text = text.replace(phrase, "")
    return [match.group() for match in CLAIM.finditer(text)
            if match.group() not in {"600+ models", "100+ providers"}]


def assert_blog_totals(post: BlogPost) -> None:
    body = post.body_html
    if post.slug == "most-llm-calls-are-decisions":
        # This SVG label counts decision models at launch, not the whole catalog.
        body = body.replace(
            '<text x="912" y="410" font-size="52" font-weight="700" fill="#111827">9 models</text>',
            "",
        )
    assert not (claims := invalid_claims(f"{post.title} {post.description} {body}")), (
        post.slug, claims,
    )


@pytest.mark.parametrize("post", BLOG_POSTS, ids=lambda post: post.slug)
def test_python_blog_content_uses_consistent_totals(post: BlogPost) -> None:
    assert_blog_totals(post)


@pytest.mark.parametrize("slug,current,old", [
    ("axios-tried-trustedrouter", "600+ models", "more than 550 models"),
    ("we-raised-1-25m-seed", "600+ models", "More than 600 models"),
    ("we-raised-1-25m-seed", "100+ providers", "More than 81 providers"),
])
def test_blog_scan_rejects_old_claims_in_memory(slug: str, current: str, old: str) -> None:
    post = BLOG_POSTS_BY_SLUG[slug]
    assert current in post.body_html
    mutated = replace(post, body_html=post.body_html.replace(current, old, 1))
    with pytest.raises(AssertionError, match=re.escape(old)):
        assert_blog_totals(mutated)


def assert_picker_totals(script: str) -> None:
    assert not (claims := invalid_claims(script)), claims
    # Catalog totals must not feed headings or template literals. Evaluated and
    # filtered-result counts use separate fields and remain exact.
    assert not (totals := re.findall(
        r"\bstate\.catalog\.catalog_(?:model|provider|route)_count\b", script,
    )), totals


def test_picker_javascript_uses_consistent_totals() -> None:
    assert_picker_totals(PICKER.read_text())


@pytest.mark.parametrize("current,old", [
    ('dom.liveCount.textContent = "600+";',
     'dom.liveCount.textContent = String(state.catalog.catalog_model_count);'),
    ('dom.providerCount.textContent = "100+";',
     'dom.providerCount.textContent = String(state.catalog.catalog_provider_count);'),
    ("a catalog of 600+ models across 100+ providers",
     "${state.catalog.catalog_route_count} live routes across "
     "${state.catalog.catalog_provider_count} providers"),
    ("600+ models", "more than 550 models"),
    ("100+ providers", "More than 81 providers"),
])
def test_picker_scan_rejects_old_claims_in_memory(current: str, old: str) -> None:
    script = PICKER.read_text()
    assert current in script
    with pytest.raises(AssertionError):
        assert_picker_totals(script.replace(current, old, 1))


def test_templates_and_homepage_content_use_consistent_totals() -> None:
    paths = [*TEMPLATES.rglob("*.html"), TEMPLATES / "homepage/content.json"]
    failures = {str(path.relative_to(TEMPLATES)): claims for path in paths
                if (claims := invalid_claims(path.read_text()))}
    assert not failures, failures
    # Dynamic totals used to bypass static phrase checks in headers and prose.
    for path in paths:
        assert not re.search(
            r"{{\s*(?:homepage_catalog.total|total_model_count|total_provider_count|"
            r"catalog_evidence\.(?:model_count|provider_count|route_count)|"
            r"region\.(?:catalog_provider_count|directory_model_count))\s*}}",
            path.read_text(),
        ), path


@pytest.mark.parametrize("current,old", [
    ("Claude and 100+ providers through", "Claude (and 30+ other providers) through"),
    ("Claude and 100+ providers, same API", "Claude + 30 more providers, same API"),
])
def test_claude_template_scan_rejects_old_claims_in_memory(current: str, old: str) -> None:
    template = (TEMPLATES / "public/seo_claude_api_privacy.html").read_text()
    assert current in template
    assert not invalid_claims(template)
    with pytest.raises(AssertionError):
        assert not invalid_claims(template.replace(current, old, 1))


def assert_static_svg_totals(text: str) -> None:
    assert not (claims := invalid_claims(text)), claims


@pytest.mark.parametrize("path", sorted(STATIC.rglob("*.svg")),
                         ids=lambda path: str(path.relative_to(STATIC)))
def test_static_svg_text_uses_consistent_totals(path: Path) -> None:
    assert_static_svg_totals(path.read_text())


def test_static_svg_scan_rejects_old_claim_in_memory() -> None:
    svg = (STATIC / "og/blog/axios-tried-trustedrouter.svg").read_text()
    assert "600+ models" in svg
    with pytest.raises(AssertionError, match=r"550\+ models"):
        assert_static_svg_totals(svg.replace("600+ models", "550+ models", 1))


@pytest.mark.parametrize("builder", [llms_txt, docs_llms_txt, docs_llms_full_txt])
def test_agent_text_builders_use_consistent_totals(builder) -> None:
    assert not invalid_claims(builder(Settings(environment="test", _env_file=None)))


def test_rendered_catalog_headings_do_not_use_live_totals(client: TestClient) -> None:
    for path in ("/", "/models", "/openrouter-alternative"):
        text = client.get(path).text
        assert "600+ models" in text
        if path != "/models":  # Catalog page has its own inline model search.
            assert 'class="search-count">600+ ' in text
        headings = " ".join(re.findall(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", text, re.S))
        assert not invalid_claims(headings)


def test_seo_template_regression_detects_old_route_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the same scanner on a real template with a reintroduced defect.
    template = TEMPLATES / "public/seo_llm_failover.html"
    mutated = tmp_path / template.name
    mutated.write_text(template.read_text().replace("600+ models", "220+ routes", 1))
    assert "220+ routes" in invalid_claims(mutated.read_text())
    (tmp_path / "homepage").mkdir()
    (tmp_path / "homepage/content.json").write_text("{}")
    monkeypatch.setattr(sys.modules[__name__], "TEMPLATES", tmp_path)
    with pytest.raises(AssertionError, match=r"220\+ routes"):
        test_templates_and_homepage_content_use_consistent_totals()


def test_competitor_columns_and_per_model_counts_are_not_catalog_totals() -> None:
    assert not invalid_claims("400+ model ids across 100+ providers")
    assert not invalid_claims("This model is served by 12 providers")
    assert not invalid_claims("12 matching models; 37 independently scored models")
    assert_picker_totals(
        "`${state.catalog.evaluated_model_count} independently scored models`; "
        "`${ranked.length} matching models`"
    )
    assert invalid_claims("TrustedRouter offers 589 models and 80 providers") == [
        "589 models", "80 providers",
    ]


def test_trustedrouter_comparison_columns_use_consistent_totals() -> None:
    from trusted_router.competitor_comparisons import COMPETITOR_COMPARISONS

    for comparison in COMPETITOR_COMPARISONS:
        for _label, _competitor, trustedrouter in comparison.rows:
            assert not invalid_claims(trustedrouter), (comparison.slug, trustedrouter)


@pytest.mark.parametrize("path", ["/models", "/providers"])
def test_catalog_json_ld_omits_exact_catalog_totals(client: TestClient, path: str) -> None:
    text = client.get(path).text
    nodes = [node for blob in re.findall(r"application/ld\+json[^>]*>(.*?)</script>", text, re.S)
             for node in json.loads(blob).get("@graph", [])]
    catalogs = [node for node in nodes if node.get("name") in {
        "TrustedRouter model catalog", "TrustedRouter provider catalog",
    }]
    assert len(catalogs) == 1
    assert "numberOfItems" not in catalogs[0]
    assert catalogs[0]["itemListElement"]  # Individual catalog rows remain discoverable.


def test_models_page_server_render_has_no_exact_catalog_total() -> None:
    # Crawlers read the server-rendered status line before the explorer script
    # replaces it with a filtered result count.
    template = (TEMPLATES / "public/models.html").read_text()
    assert "{{ models|length }} models" not in template
    assert not re.search(r"\{\{[^}]*\}\}\s*models</strong><span[^>]*>\s*in the public catalog", template)
    assert '600+ models</strong><span data-model-result-detail> in the public catalog' in template
