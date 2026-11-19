"""Marketing totals are stable; competitor facts and per-route evidence stay exact."""
from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.dashboard import docs_llms_full_txt, docs_llms_txt, llms_txt

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/trusted_router/templates"
CLAIM = re.compile(
    r"\b(?:more than |over )?\d[\d,]*\+?\s+(?:(?:additional|public|AI|LLM)\s+)?"
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
