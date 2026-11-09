"""The catalog hero: one page-specific reason to sign up on model, comparison
and provider pages, opening the shared dialog in sign-up mode.

The "Free to start" label rests on the $0.30 starter credit, which only pays
for Credits routes, so a page whose model has none says "Bring your own key"
and makes no credit claim.
"""

from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router import dashboard
from trusted_router.catalog import MODELS, PROVIDERS, endpoints_for_model
from trusted_router.config import Settings
from trusted_router.main import create_app

CTA = 'data-action="open-signin" data-signin-mode="signup" data-catalog-cta'
GENERIC_TILE = "base URL to migrate"


@pytest.fixture
def client(test_settings: Settings) -> TestClient:
    settings = test_settings.model_copy(update={"rate_limit_enabled": False})
    return TestClient(create_app(settings, init_observability=False))


def _hero(html: str) -> str:
    match = re.search(r'<section class="public-hero catalog-hero">.*?</section>', html, re.S)
    assert match, "catalog hero missing"
    return match.group(0)


def _credits_route_count(model_id: str) -> int:
    return len(dashboard._credits_endpoints(endpoints_for_model(model_id)))


def _first_model(*, funded: bool) -> str:
    for model in MODELS.values():
        if model.internal_only or model.id in dashboard.META_MODEL_IDS:
            continue
        if (_credits_route_count(model.id) > 0) is funded:
            return model.id
    raise AssertionError("catalog has no such model")


def _model_hero(html: str) -> str:
    match = re.search(r'<section class="md-hero".*?</section>', html, re.S)
    assert match, "model hero missing"
    return match.group(0)


NO_CARD = "No card required. Start with 1&nbsp;million free&nbsp;tokens."


def test_funded_model_page_is_free_to_start(client: TestClient) -> None:
    model_id = _first_model(funded=True)
    response = client.get(f"/models/{model_id}")
    assert response.status_code == 200
    hero = _model_hero(response.text)
    assert CTA in hero
    assert ">Create my API key</button>" in hero
    assert hero.count("data-catalog-cta") == 1
    assert NO_CARD in hero
    assert GENERIC_TILE not in response.text
    assert "/console" not in hero
    # The no-card line is the approved homepage string; the rest of the hero
    # keeps to the public-copy rule.
    text = re.sub(r"<[^>]+>", " ", hero.replace(NO_CARD, ""))
    assert not re.search(r"\b(no|not|never)\b", text, re.I)


def test_byok_only_model_page_makes_no_credit_claim(client: TestClient) -> None:
    model_id = _first_model(funded=False)
    response = client.get(f"/models/{model_id}")
    assert response.status_code == 200
    hero = _model_hero(response.text)
    assert "Bring your own provider key" in hero
    assert "free&nbsp;tokens" not in response.text
    assert "on us" not in hero
    assert CTA in hero


def test_model_section_pages_share_the_model_hero(client: TestClient) -> None:
    model_id = _first_model(funded=True)
    response = client.get(f"/models/{model_id}/pricing")
    assert response.status_code == 200
    hero = _hero(response.text)
    assert '<div class="kicker">Free to start</div>' in hero
    assert CTA in hero


def test_model_page_heading_drops_the_publisher_prefix(client: TestClient) -> None:
    model = next(
        m for m in MODELS.values()
        if not m.internal_only and ": " in m.name and _credits_route_count(m.id) > 0
    )
    response = client.get(f"/models/{model.id}")
    short = model.name.split(": ", 1)[1]
    hero = _model_hero(response.text)
    assert f'<h1 id="md-title">{short}</h1>' in hero
    assert f"<title>{dashboard._seo_model_name(model)}" in response.text


def test_meta_model_page_is_free_to_start(client: TestClient) -> None:
    response = client.get("/models/trustedrouter/auto")
    assert response.status_code == 200
    hero = _model_hero(response.text)
    assert NO_CARD in hero
    assert CTA in hero


def test_model_page_agent_prompt_names_the_model(client: TestClient) -> None:
    response = client.get("/models/deepseek/deepseek-v3.2")
    assert response.status_code == 200
    match = re.search(r'<code id="md-agent-prompt">(.*?)</code>', response.text, re.S)
    assert match
    prompt = re.sub(r"<[^>]+>", "", match.group(1))
    assert prompt.startswith("Use DeepSeek V3.2 through TrustedRouter in this project.")
    assert "Set the model to deepseek/deepseek-v3.2." in prompt
    assert "https://api.trustedrouter.com/v1" in prompt
    assert 'aria-label="Copy"' in response.text


def _compare_hero(html: str) -> str:
    match = re.search(r'<section class="compare-hero">.*?</section>', html, re.S)
    assert match, "comparison hero missing"
    return match.group(0)


def test_comparison_page_label_follows_both_models(client: TestClient) -> None:
    funded = _first_model(funded=True)
    byok = _first_model(funded=False)
    second = next(
        m.id for m in MODELS.values()
        if not m.internal_only
        and m.id not in dashboard.META_MODEL_IDS
        and m.id != funded
        and _credits_route_count(m.id) > 0
    )
    both = client.get(f"/compare/models/{funded}/vs/{second}", follow_redirects=True)
    assert both.status_code == 200
    hero = _compare_hero(both.text)
    assert "Start with 1 million free tokens." in hero
    assert CTA in hero
    assert hero.count("data-catalog-cta") == 1

    # A pair with a bring-your-own-key model makes no credit claim.
    mixed = client.get(f"/compare/models/{funded}/vs/{byok}", follow_redirects=True)
    assert mixed.status_code == 200
    hero = _compare_hero(mixed.text)
    assert "free tokens" not in hero
    assert "Try both on us." not in hero
    assert "runs on your own provider key" in hero


def test_provider_page_is_free_to_start_when_it_has_credits_routes(client: TestClient) -> None:
    slug = next(
        p.slug for p in PROVIDERS.values()
        if not p.internal_only and p.slug != "trustedrouter"
        and dashboard._provider_model_rows(p.slug, test_mode=True)
    )
    response = client.get(f"/providers/{slug}")
    assert response.status_code == 200
    hero = _hero(response.text)
    assert '<div class="kicker">Free to start</div>' in hero
    assert " on us. Starter credit covers your first requests" in hero
    assert CTA in hero
    assert GENERIC_TILE not in response.text


def _provider_rows(html: str) -> list[str]:
    # Main provider rows only, not their detail rows.
    return re.findall(r'<tr class="md-provider(?: [^"]*)?"[^>]*>', html)


def test_model_page_tags_every_row_tied_for_cheapest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = "deepseek/deepseek-v3.2"
    real = dashboard._model_detail_view

    def view_with_prices(model, **kwargs):  # type: ignore[no-untyped-def]
        view = real(model, **kwargs)
        for index, endpoint in enumerate(view["endpoints"]):
            endpoint["prompt_microdollars_per_million_tokens"] = 100_000 if index < 2 else 300_000
            endpoint["completion_microdollars_per_million_tokens"] = 200_000
        return view

    monkeypatch.setattr(dashboard, "_model_detail_view", view_with_prices)
    rows = _provider_rows(client.get(f"/models/{model_id}").text)
    assert len(rows) >= 3
    assert ["md-provider-cheapest" in row for row in rows[:3]] == [True, True, False]


def test_model_page_tags_no_row_when_every_row_costs_the_same(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = dashboard._model_detail_view

    def flat_prices(model, **kwargs):  # type: ignore[no-untyped-def]
        view = real(model, **kwargs)
        for endpoint in view["endpoints"]:
            endpoint["prompt_microdollars_per_million_tokens"] = 100_000
            endpoint["completion_microdollars_per_million_tokens"] = 200_000
        return view

    monkeypatch.setattr(dashboard, "_model_detail_view", flat_prices)
    html = client.get("/models/deepseek/deepseek-v3.2").text
    assert _provider_rows(html)
    assert "md-provider-cheapest" not in html
    assert "md-cheapest-tag" not in html


def test_model_page_sort_control_offers_only_measured_orders(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = "deepseek/deepseek-v3.2"
    html = client.get(f"/models/{model_id}").text
    assert 'data-md-sort-key="price"' in html
    assert 'data-md-sort-key="throughput"' not in html
    assert 'data-md-sort-key="latency"' not in html

    provider = dashboard._credits_endpoints(endpoints_for_model(model_id))[0].provider
    measured = [{
        "provider": provider,
        "p50_ttft_ms": 412.4,
        "ttft_sample_count": 5,
        "p50_tokens_per_second": 61.26,
        "throughput_sample_count": 5,
    }]
    monkeypatch.setattr(dashboard, "measured_for_model", lambda *_a, **_k: measured)
    html = client.get(f"/models/{model_id}").text
    assert 'data-md-sort-key="throughput"' in html
    assert 'data-md-sort-key="latency"' in html
    row = next(r for r in _provider_rows(html) if f'id="provider-{provider}"' in r)
    assert 'data-sort-throughput="61.3"' in row
    assert 'data-sort-latency="412"' in row


def test_model_page_links_the_live_leaderboard(client: TestClient) -> None:
    html = client.get(f"/models/{_first_model(funded=True)}").text
    providers = re.search(r'<section class="md-section" id="providers".*?</section>', html, re.S)
    assert providers
    intro = re.search(r'<p class="md-intro">.*?</p>', providers.group(0), re.S)
    assert intro, "provider intro missing"
    assert '<a class="md-inline-link" href="/trust">how we check privacy claims</a>' in intro.group(0)
    assert '<a class="md-inline-link" href="/leaderboard">live speed leaderboard</a>' in intro.group(0)
    assert "md-more-links" not in html
    assert "→" not in providers.group(0)


def test_model_page_lists_other_model_pages_as_a_fact(client: TestClient) -> None:
    html = client.get("/models/deepseek/deepseek-v3.2").text
    about = re.search(r'<section class="md-section" id="about".*?</section>', html, re.S)
    assert about
    fact = re.search(r'<dt>More data</dt><dd class="md-more-data">(.*?)</dd>', about.group(0), re.S)
    assert fact, "More data fact missing"
    assert '<a class="md-inline-link" href="/models/deepseek/deepseek-v3.2/benchmarks"><span class="md-sr">DeepSeek V3.2 </span>Benchmarks</a>' in fact.group(1)
    assert '<span class="md-dot" aria-hidden="true"> · </span>' in fact.group(1)
    assert "→" not in about.group(0)


def test_model_page_puts_about_before_quickstart_and_providers(client: TestClient) -> None:
    html = client.get("/models/deepseek/deepseek-v3.2").text
    order = [html.index(f'id="{section}"') for section in ("about", "quickstart", "providers", "faq")]
    assert order == sorted(order)


def test_model_page_provider_list_is_a_table(client: TestClient) -> None:
    html = client.get("/models/deepseek/deepseek-v3.2").text
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.md-provider-table")
    assert table is not None
    assert table.caption.get_text() == "DeepSeek V3.2 providers and prices per 1M tokens"
    headers = [th.get_text(strip=True) for th in table.select('thead th[scope="col"]')]
    assert headers == ["Provider", "Input", "Cached", "Output", "Cost per 1M in + out", "Details"]
    rows = table.select("tbody > tr.md-provider")
    assert rows and "md-provider-table--sticky" not in table["class"]
    for row in rows:
        assert row.select_one('th[scope="row"]')
        button = row.select_one("button.md-toggle")
        assert button["aria-expanded"] == "false"
        detail = row.find_next_sibling("tr")
        assert "md-provider-detail" in detail["class"]
        assert button["aria-controls"] == detail["id"] == f'{row["id"]}-details'
        assert detail.td["colspan"] == "6"
        assert "Weight format" in detail.get_text()


def test_model_page_sticks_the_table_header_past_ten_providers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = dashboard._model_detail_view

    def many_rows(model, **kwargs):  # type: ignore[no-untyped-def]
        view = real(model, **kwargs)
        endpoint = view["endpoints"][0]
        view["endpoints"] = [
            {**endpoint, "provider_slug": f"{endpoint['provider_slug']}-{index}"} for index in range(11)
        ]
        return view

    monkeypatch.setattr(dashboard, "_model_detail_view", many_rows)
    html = client.get("/models/deepseek/deepseek-v3.2").text
    assert "md-provider-table--sticky" in html


def test_model_page_shows_the_ai_iq_score(client: TestClient) -> None:
    html = client.get("/models/openai/gpt-5.5").text
    about = re.search(r'<p class="md-about">.*?</p>', html, re.S)
    assert about
    assert "It scores 128 on" in about.group(0)
    assert 'href="https://aiiq.org/models/gpt-5.5/" rel="nofollow noopener"' in about.group(0)


def test_model_page_provider_details_use_labels_not_headings(client: TestClient) -> None:
    html = client.get("/models/deepseek/deepseek-v3.2").text
    details = re.findall(r'<div class="md-provider-details">.*?</tr>', html, re.S)
    assert details
    assert all("<h3" not in block for block in details)
    assert '<p class="md-detail-label">Weight format</p>' in details[0]


def test_readable_no_card_note_has_its_own_class(client: TestClient) -> None:
    models_html = client.get("/models").text
    assert '<p class="cta-note cta-note-readable">No card required.' in models_html
    slug = re.search(r'href="/blog/([^"]+)"', client.get("/blog").text)
    assert slug
    blog_html = client.get(f"/blog/{slug.group(1)}").text
    assert '<span class="cta-note cta-note-readable">No card required.' in blog_html
    for html in (models_html, blog_html):
        assert "microcopy cta-note" not in html
