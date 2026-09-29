from __future__ import annotations

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router import dashboard
from trusted_router.catalog import MODELS, Model
from trusted_router.config import Settings
from trusted_router.homepage import homepage_context


def test_homepage_rollout_is_explicit_and_alternate_brands_unchanged() -> None:
    settings = Settings(environment="test", storage_backend="memory")
    assert not settings.homepage_landscape_enabled
    assert 'class="hero-promises"' not in dashboard.dashboard_html(settings)
    settings.homepage_landscape_enabled = True
    assert 'class="hero-promises"' in dashboard.dashboard_html(settings)
    alternate = dashboard.dashboard_html(settings, brand_name="UptimeRouter")
    assert 'class="hero-promises"' not in alternate
    assert "UptimeRouter" in alternate


def test_landscape_root_integrates_catalog_assets_csp_and_signin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "homepage_landscape_enabled", True)
    response = client.get("/")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    for section in ("top", "request", "models", "migrate", "verify", "pricing", "customers", "faq"):
        assert page.select_one(f"section#{section}")
    assert len(page.select("[data-mobile-disclosure][open]")) == 11
    assert page.select_one("#signinModal")
    assert page.select_one('a[href="/console/api-keys"]')
    assert page.select_one('a[href="/models"]')
    for script in page.select("script:not([src])"):
        assert script.get("nonce")
    runtime = json.loads(page.select_one("#homepage-data").string)
    assert "catalog" not in runtime  # no fixture prices in the browser payload
    assert runtime["catalog_total"] == len(client.get("/v1/models/picker").json()["data"])
    assert "Illustrative request" in page.get_text()
    assert "All systems operational" not in page.get_text()
    for image in page.select("img[src^='/static/homepage/']"):
        assert client.get(image["src"]).status_code == 200


def test_catalog_prices_and_privacy_follow_application_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = dashboard._model_view

    def changed(model: Model, *, test_mode: bool = False) -> dict[str, object]:
        result = original(model, test_mode=test_mode)
        if model.id == "z-ai/glm-5.3-flash":
            result.update(prompt_price_sort=1234567, prompt_price="$1.234567/1M",
                          zdr_available=False, e2e_available=False, provider_count=7)
        return result

    monkeypatch.setattr(dashboard, "_model_view", changed)
    result = homepage_context("https://example.test/v1")
    row = next(row for row in result["homepage_catalog"]["lists"]["all"]
               if row["id"] == "z-ai/glm-5.3-flash")
    assert row["input_price"] == "$1.234567"
    assert row["provider_count"] == 7
    assert row["route_labels"] == []
    for key in ("z", "c"):
        assert all(row["id"] != "z-ai/glm-5.3-flash"
                   for row in result["homepage_catalog"]["lists"][key])
    assert result["homepage_data"]["migration"]["base_url"] == "https://example.test/v1"


def test_homepage_catalog_matches_model_directory() -> None:
    result = homepage_context("https://api.trustedrouter.com/v1")
    for key, rows in result["homepage_catalog"]["lists"].items():
        for row in rows:
            view = dashboard._model_view(MODELS[row["id"]], test_mode=True)
            assert row["provider_count"] == view["provider_count"]
            assert ("ZDR" in row["route_labels"]) == view["zdr_available"]
            assert ("E2EE" in row["route_labels"]) == view["e2e_available"]
            if key == "z":
                assert view["zdr_available"]
            if key == "c":
                assert view["e2e_available"]
    script = Path("src/trusted_router/static/homepage/homepage.js").read_text()
    assert "fetch('/v1/models/picker'" in script
    assert "AbortSignal.timeout" in script
