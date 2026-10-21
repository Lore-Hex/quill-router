"""The catalog hero: one page-specific reason to sign up on model, comparison
and provider pages, opening the shared dialog in sign-up mode.

The "Free to start" label rests on the $0.30 starter credit, which only pays
for Credits routes, so a page whose model has none says "Bring your own key"
and makes no credit claim.
"""

from __future__ import annotations

import re

import pytest
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


def test_funded_model_page_is_free_to_start(client: TestClient) -> None:
    model_id = _first_model(funded=True)
    response = client.get(f"/models/{model_id}")
    assert response.status_code == 200
    hero = _hero(response.text)
    assert '<div class="kicker">Free to start</div>' in hero
    assert " on us. Starter credit covers your first requests" in hero
    assert CTA in hero
    assert hero.count("data-catalog-cta") == 1
    assert GENERIC_TILE not in response.text
    assert "/console" not in hero
    assert not re.search(r"\b(no|not|never)\b", re.sub(r"<[^>]+>", " ", hero), re.I)


def test_byok_only_model_page_makes_no_credit_claim(client: TestClient) -> None:
    model_id = _first_model(funded=False)
    response = client.get(f"/models/{model_id}")
    assert response.status_code == 200
    hero = _hero(response.text)
    assert '<div class="kicker">Bring your own key</div>' in hero
    assert "Starter credit" not in hero
    assert "on us" not in hero
    assert CTA in hero


def test_model_section_pages_share_the_model_hero(client: TestClient) -> None:
    model_id = _first_model(funded=True)
    response = client.get(f"/models/{model_id}/pricing")
    assert response.status_code == 200
    hero = _hero(response.text)
    assert '<div class="kicker">Free to start</div>' in hero
    assert CTA in hero


def test_hero_sentence_drops_the_publisher_prefix(client: TestClient) -> None:
    model = next(
        m for m in MODELS.values()
        if not m.internal_only and ": " in m.name and _credits_route_count(m.id) > 0
    )
    response = client.get(f"/models/{model.id}")
    short = model.name.split(": ", 1)[1]
    hero = _hero(response.text)
    assert f"<h1>{model.name}</h1>" in hero
    assert f"Try {short} on us." in hero
    assert f"Try {model.name} on us." not in hero


def test_meta_model_page_is_free_to_start(client: TestClient) -> None:
    response = client.get("/models/trustedrouter/auto")
    assert response.status_code == 200
    hero = _hero(response.text)
    assert '<div class="kicker">Free to start</div>' in hero
    assert "Starter credit covers your first requests" in hero


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
    hero = _hero(both.text)
    assert '<div class="kicker">Free to start</div>' in hero
    assert "Try both on us." in hero
    assert CTA in hero

    mixed = client.get(f"/compare/models/{funded}/vs/{byok}", follow_redirects=True)
    assert mixed.status_code == 200
    hero = _hero(mixed.text)
    assert '<div class="kicker">Bring your own key</div>' in hero
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
