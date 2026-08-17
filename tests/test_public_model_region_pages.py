"""Guards for the three jurisdiction directories.

The load-bearing property is that these pages keep two facts apart: the country
of the lab that built a model, and the country of the company operating the
endpoint a request reaches. Conflating them would produce copy that is false for
most of the catalog, so the tests below check the separation on live catalog
data rather than checking that some words are present.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.exceptions import HTTPException
from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS, PROVIDERS, endpoints_for_model
from trusted_router.catalog_data import (
    PROVIDER_JURISDICTION_CN,
    PROVIDER_JURISDICTION_US,
    model_origin_for_model_id,
)
from trusted_router.dashboard import PUBLIC_PAGES
from trusted_router.model_regions import (
    LIBERTY_MODEL_IDS,
    MODEL_REGION_SLUGS,
    liberty_component_origin_counts,
    model_region_evidence,
)
from trusted_router.routing import _provider_jurisdiction

REGION_PATHS = tuple(f"/{slug}" for slug in MODEL_REGION_SLUGS)
TEMPLATE_DIR = Path("src/trusted_router/templates/public")
REGION_TEMPLATES = (
    "_model_region_directory.html",
    "seo_us_ai_models.html",
    "seo_eu_ai_models.html",
    "seo_china_ai_models.html",
)


def _json_ld(html: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/ld\+json">(?P<payload>.*?)</script>',
        html,
    )
    assert match is not None
    payload = json.loads(match.group("payload"))
    assert isinstance(payload, dict)
    return payload


def _region_copy() -> str:
    """Every word these pages author themselves: the four templates plus the
    title, description, and FAQ text registered for the three slugs. Site
    furniture rendered around them (nav, footer, shared partials) belongs to
    other pages and is not this guard's subject."""
    parts = [(TEMPLATE_DIR / name).read_text() for name in REGION_TEMPLATES]
    for slug in MODEL_REGION_SLUGS:
        page = PUBLIC_PAGES[slug]
        parts.append(page.title)
        parts.append(page.description)
        parts.extend(text for pair in page.faq_items for text in pair)
    return "\n".join(parts)


@pytest.mark.parametrize("path", REGION_PATHS)
def test_region_pages_declare_canonical_and_structured_data(
    client: TestClient,
    path: str,
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.text.count('rel="canonical"') == 1
    assert f'<link rel="canonical" href="https://trustedrouter.com{path}">' in response.text
    payload = _json_ld(response.text)
    types = {item["@type"] for item in payload["@graph"]}
    assert {"BreadcrumbList", "WebPage", "ItemList", "FAQPage"}.issubset(types)
    lists = [item for item in payload["@graph"] if item["@type"] == "ItemList"]
    assert any(int(item["numberOfItems"]) > 0 for item in lists)
    assert client.head(path).status_code == 200
    assert client.get(f"{path}/", follow_redirects=False).status_code == 200


def test_region_pages_list_only_providers_from_that_jurisdiction() -> None:
    """The provider table is the "where do my prompts go" axis. A row on it is a
    claim about the operator's legal home, so every row must come from the
    provider_headquarters_country field and nothing else."""
    for slug in MODEL_REGION_SLUGS:
        evidence = model_region_evidence(slug)
        rows = evidence["provider_rows"]
        assert isinstance(rows, list)
        assert rows, slug
        for row in rows:
            provider = PROVIDERS[str(row["slug"])]
            assert row["country_code"] == provider.provider_headquarters_country, (slug, row)
            assert provider.provider_headquarters_country is not None, (slug, row)


def test_region_pages_group_models_by_lab_origin_not_by_serving_provider() -> None:
    """The lab sections are the "who built this" axis. Every model under a lab
    heading must carry that origin country in MODEL_ORIGINS, whoever serves it."""
    expected_countries = {
        "us-ai-models": {PROVIDER_JURISDICTION_US},
        "china-ai-models": {PROVIDER_JURISDICTION_CN},
        "eu-ai-models": {"FR", "NL", "SE", "DE", "IE", "IT", "ES", "PL", "FI", "SI"},
    }
    for slug in MODEL_REGION_SLUGS:
        evidence = model_region_evidence(slug)
        labs = evidence["labs"]
        assert isinstance(labs, list)
        assert labs, slug
        for lab in labs:
            for model in lab["models"]:
                origin = model_origin_for_model_id(str(model["id"]))
                assert origin is not None, (slug, model["id"])
                assert origin.lab_name == lab["lab_name"], (slug, model["id"])
                assert origin.country in expected_countries[slug], (slug, model["id"])


def test_us_page_features_every_liberty_route_with_a_computed_origin_count(
    client: TestClient,
) -> None:
    """Liberty earns its place on the US page arithmetically. If a component
    were swapped for a model from a non-US lab, the count would stop matching
    and this test would fail rather than the page shipping a stale claim."""
    response = client.get("/us-ai-models")

    assert response.status_code == 200
    for model_id in LIBERTY_MODEL_IDS:
        assert model_id in response.text, model_id
        counts = liberty_component_origin_counts(model_id)
        total = sum(counts.values())
        assert total > 0, model_id
        assert counts.get(PROVIDER_JURISDICTION_US, 0) == total, (model_id, counts)
        assert f"{total} of {total}" in response.text, model_id


def test_china_page_shows_a_chinese_model_with_its_us_operated_routes(
    client: TestClient,
) -> None:
    """The honesty claim the page is built around: a Chinese-lab model reachable
    through a US-operated provider, with that provider named on the row."""
    response = client.get("/china-ai-models")
    assert response.status_code == 200

    evidence = model_region_evidence("china-ai-models")
    labs = evidence["labs"]
    assert isinstance(labs, list)
    shown = [model for lab in labs for model in lab["models"]]
    with_us_routes = [model for model in shown if model["highlight_operators"]]
    assert with_us_routes, "no Chinese-lab model on the page has a US-operated route"

    for model in with_us_routes:
        assert f'href="/models/{model["id"]}"' in response.text, model["id"]
        for operator in model["highlight_operators"]:
            slug = str(operator["slug"])
            assert PROVIDERS[slug].provider_headquarters_country == PROVIDER_JURISDICTION_US
            assert f'href="/providers/{slug}"' in response.text, (model["id"], slug)
            # The same weights, served by a company registered outside China.
            assert any(
                endpoint.provider == slug for endpoint in endpoints_for_model(str(model["id"]))
            )

    # And the vendor-endpoint side of the same story is on the page too.
    china_providers = evidence["provider_rows"]
    assert isinstance(china_providers, list)
    for row in china_providers:
        assert f'href="/providers/{row["slug"]}"' in response.text, row["slug"]


def test_region_pages_report_operator_jurisdiction_per_model(client: TestClient) -> None:
    """Every model row carries the full operator breakdown, not only the
    flattering half: a Chinese-lab model served from China says so."""
    evidence = model_region_evidence("china-ai-models")
    labs = evidence["labs"]
    assert isinstance(labs, list)
    for lab in labs:
        for model in lab["models"]:
            operators = {str(operator["slug"]) for operator in model["operators"]}
            configured = {
                endpoint.provider for endpoint in endpoints_for_model(str(model["id"]))
            }
            assert operators <= configured, model["id"]
            counted = sum(int(str(chip["operator_count"])) for chip in model["jurisdiction_chips"])
            assert counted == len(configured), model["id"]


def test_sitemap_and_navigation_carry_each_region_page_once(client: TestClient) -> None:
    sitemap = client.get("/sitemap-core.xml")
    assert sitemap.status_code == 200
    for path in REGION_PATHS:
        assert sitemap.text.count(f"<loc>https://trustedrouter.com{path}</loc>") == 1, path

    models = client.get("/models")
    assert models.status_code == 200
    for path in REGION_PATHS:
        assert f'href="{path}"' in models.text, path

    llms = client.get("/llms.txt")
    assert llms.status_code == 200
    for path in REGION_PATHS:
        assert f"https://trustedrouter.com{path}" in llms.text, path


def test_region_pages_cross_link_each_other(client: TestClient) -> None:
    for path in REGION_PATHS:
        response = client.get(path)
        assert response.status_code == 200
        for other in REGION_PATHS:
            if other == path:
                continue
            assert f'href="{other}"' in response.text, (path, other)


def test_eu_page_does_not_promise_a_jurisdiction_filter_that_does_not_exist() -> None:
    """The EU page tells readers to use provider.only because
    provider.jurisdiction accepts 'us' and nothing else. If that ever changes,
    this fails and the copy gets revisited instead of going stale."""
    assert _provider_jurisdiction("us") == PROVIDER_JURISDICTION_US
    for value in ("eu", "europe", "cn", "de"):
        with pytest.raises(HTTPException):
            _provider_jurisdiction(value)


def test_region_page_copy_avoids_banned_words_and_the_open_source_claim() -> None:
    """House style bans, plus the one that matters most here: the gateway is
    source-available under BUSL-1.1, so no page may call it open source. These
    pages talk about open-weights models constantly, which is a different thing
    and must not slide into the wrong phrase."""
    copy = _region_copy().lower()
    banned_words = (
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
    for word in banned_words:
        assert word not in copy, f"banned word {word!r}"
    for phrase in ("open source", "open-source"):
        assert phrase not in copy, f"BUSL-1.1 is source-available: {phrase!r}"


def test_region_pages_never_turn_operator_country_into_a_residency_claim(
    client: TestClient,
) -> None:
    """A jurisdiction field records where a company is registered. Every one of
    these pages has to say, in its own words, that this is not where the
    hardware is."""
    for path in REGION_PATHS:
        response = client.get(path)
        assert response.status_code == 200
        assert "datacentre" in response.text, path
        forbidden = (
            "data never leaves",
            "guaranteed data residency",
            "prompts stay in the",
        )
        for claim in forbidden:
            assert claim not in response.text.lower(), (path, claim)


def test_liberty_routes_are_meta_routes_the_catalog_actually_defines() -> None:
    """The featured list is written from catalog constants; a Liberty id retired
    from the catalog must not keep a section on the page."""
    for model_id in LIBERTY_MODEL_IDS:
        assert model_id in MODELS, model_id


def test_liberty_leads_the_us_page_and_is_framed_as_open_weight(client: TestClient) -> None:
    """Liberty is the first thing on /us-ai-models, above the lab catalogue.

    Every Liberty route is open weight -- `model_open_weights` is recursive and
    every component under every Liberty id qualifies -- so it belongs at the
    top of a page about US open-weight models rather than after them. Ordering
    is asserted by position because a heading can be renamed without moving,
    and the point here is which section a reader meets first.
    """
    body = client.get("/us-ai-models").text

    liberty_at = body.find('aria-label="Liberty model family"')
    open_weight_at = body.find('aria-label="US open-weight models"')
    assert liberty_at != -1, "Liberty panel missing from /us-ai-models"
    assert open_weight_at != -1, "open-weight panel missing from /us-ai-models"
    assert liberty_at < open_weight_at, "Liberty must come before the lab open-weight list"
    assert "open weight" in body[liberty_at : liberty_at + 400].lower()


def test_every_liberty_route_really_is_open_weight() -> None:
    """The claim the panel makes, checked against the catalog rather than the copy.

    If a Liberty component were ever swapped for a closed-weights model this
    fails, which is what lets the page state it without hedging.
    """
    from trusted_router.catalog import MODELS, model_open_weights

    models = MODELS if isinstance(MODELS, (list, tuple)) else list(MODELS.values())
    liberty = [model for model in models if "liberty" in model.id.lower()]

    assert liberty, "no Liberty routes found in the catalog"
    not_open = [model.id for model in liberty if not model_open_weights(model)]
    assert not not_open, f"Liberty routes that are not open weight: {not_open}"
