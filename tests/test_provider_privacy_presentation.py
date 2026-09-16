from dataclasses import replace
from itertools import product

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.dashboard import _endpoint_provider_views, _provider_view


@pytest.mark.parametrize("compute,e2ee,zdr", tuple(product((True, False, None), repeat=3)))
def test_confidential_verification_requires_both_provider_flags(
    compute: bool | None, e2ee: bool | None, zdr: bool | None
) -> None:
    provider = replace(
        PROVIDERS["phala"],
        provider_confidential_compute=compute,
        provider_e2ee=e2ee,
        provider_zero_data_retention=zdr,
        attested_gateway=True,
    )
    view = _provider_view(provider)
    verified = compute is True and e2ee is True
    assert view["confidential_inference_verified"] is verified
    assert view["confidential_inference_label"] == (
        "Verified" if verified else "Not verified"
    )
    assert (view["privacy_tier"] == "Confidential") is verified
    assert view["privacy_tier"] != "Confidential compute"


def test_provider_cards_compare_upstream_verification_not_gateway(client: TestClient) -> None:
    response = client.get("/providers")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select("[data-provider-row]")
    assert {card["data-provider-id"] for card in cards} == set(PROVIDERS) - {"trustedrouter"}
    assert soup.select_one("[data-provider-result-count]").get_text(strip=True) == f"{len(cards)} entries"
    assert cards[0]["data-provider-id"] == "tinfoil"
    for card in cards:
        provider = PROVIDERS[str(card["data-provider-id"])]
        facts = {
            row.dt.get_text(strip=True): row.dd.get_text(" ", strip=True)
            for row in card.select(".provider-card-facts > div")
        }
        assert "Confidential compute" not in facts
        assert "Provider E2EE" not in facts
        assert "Zero data retention" in facts
        verified = provider.provider_confidential_compute is True and provider.provider_e2ee is True
        assert facts["Verified confidential inference"] == ("Verified" if verified else "Not verified")


@pytest.mark.parametrize("slug", ["phala", "confidential-ai", "zero-g", "openai", "tinfoil"])
def test_provider_details_use_the_same_verification_rule(client: TestClient, slug: str) -> None:
    response = client.get(f"/providers/{slug}")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    facts = {
        row.th.get_text(strip=True): row.td.get_text(" ", strip=True)
        for row in soup.select(".panel-body table")[0].select("tr")
    }
    assert "Confidential compute" not in facts
    assert "Provider E2EE" not in facts
    provider = PROVIDERS[slug]
    verified = provider.provider_confidential_compute is True and provider.provider_e2ee is True
    assert facts["Verified confidential inference"] == ("Verified" if verified else "Not verified")


def test_gateway_detail_does_not_claim_all_upstream_inference_is_verified(client: TestClient) -> None:
    response = client.get("/providers/trustedrouter")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    labels = {cell.get_text(strip=True) for cell in soup.select(".panel-body table th")}
    assert "Confidential compute" not in labels
    assert "Provider E2EE" not in labels
    assert "Verified confidential inference" not in labels
    assert "Gateway only" in response.text


def test_provider_cards_show_independent_scoped_privacy_badges(client: TestClient) -> None:
    soup = BeautifulSoup(client.get("/providers").text, "html.parser")
    assert soup.select_one("select[data-provider-privacy]") is not None
    for card in soup.select("[data-provider-row]"):
        provider = PROVIDERS[str(card["data-provider-id"])]
        confidential = provider.provider_confidential_compute is True and provider.provider_e2ee is True
        zdr = provider.provider_zero_data_retention is True or provider.prepaid_zero_data_retention
        badges = card.select_one(".provider-card-trust")
        assert bool(badges.select('[data-privacy="confidential"]')) is confidential
        assert bool(badges.select('[data-privacy="zdr"]')) is zdr
        assert card["data-confidential"] == str(confidential).lower()
        assert card["data-zdr"] == str(zdr).lower()
        if zdr and provider.provider_zero_data_retention is not True:
            assert "TR-funded" in badges.select_one('[data-privacy="zdr"]').get_text()


def test_model_cards_put_privacy_next_to_the_model_name(client: TestClient) -> None:
    from trusted_router.catalog import MODELS
    from trusted_router.dashboard import _model_view

    soup = BeautifulSoup(client.get("/models").text, "html.parser")
    confidential_count = zdr_count = 0
    for card in soup.select("[data-model-card]"):
        view = _model_view(MODELS[str(card["data-model-id"])], test_mode=True)
        heading = card.select_one(".model-card-heading")
        assert bool(heading.select('[data-privacy="confidential"]')) is view["e2e_available"]
        assert bool(heading.select('[data-privacy="zdr"]')) is view["zdr_available"]
        if view["e2e_available"] or view["zdr_available"]:
            assert ("Component routes" if view["is_meta"] else "Available routes") in heading.get_text()
        confidential_count += bool(view["e2e_available"])
        zdr_count += bool(view["zdr_available"])
    assert confidential_count > 0
    assert zdr_count > confidential_count
    assert "TR router attested" not in soup.get_text()


def test_serving_provider_badges_respect_exact_route_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        PROVIDERS, "phala", replace(PROVIDERS["phala"], provider_e2ee=True,
                                   provider_confidential_compute=True, provider_zero_data_retention=True)
    )
    base = next(iter(MODEL_ENDPOINTS.values()))
    passthrough = replace(base, provider="phala", usage_type="Credits", upstream_id="openai/gpt-5")
    confidential = replace(passthrough, upstream_id="phala/test-model")
    views = _endpoint_provider_views([passthrough], fallback_provider="phala")
    assert views[0]["confidential_available"] is False
    assert views[0]["zdr_available"] is False
    views = _endpoint_provider_views([confidential, passthrough], fallback_provider="phala")
    assert len(views) == 1
    assert views[0]["confidential_available"] is True
    assert views[0]["zdr_available"] is True
    views = _endpoint_provider_views([], fallback_provider="phala")
    assert views[0]["confidential_available"] is False
    assert views[0]["zdr_available"] is False


def test_serving_provider_badges_keep_byok_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROVIDERS, "openai", replace(
        PROVIDERS["openai"], provider_zero_data_retention=False, prepaid_zero_data_retention=True,
        provider_confidential_compute=False, provider_e2ee=False,
    ))
    base = replace(next(iter(MODEL_ENDPOINTS.values())), provider="openai", model_id="openai/test")
    for usage, expected in [("Credits", True), ("BYOK", False)]:
        views = _endpoint_provider_views([replace(base, usage_type=usage)], fallback_provider="openai")
        assert views[0]["zdr_available"] is expected
        assert views[0]["confidential_available"] is False


@pytest.mark.parametrize("compute,e2ee,zdr", tuple(product((True, False, None), repeat=3)))
def test_detail_route_badges_use_both_verification_flags(
    compute: bool | None, e2ee: bool | None, zdr: bool | None
) -> None:
    from trusted_router.dashboard import _env

    template = _env().get_template("public/_provider_privacy_badges.html")
    html = template.module.provider_privacy_badges({
        "provider_confidential_compute": compute,
        "provider_e2ee": e2ee,
        "provider_zero_data_retention": zdr,
    })
    soup = BeautifulSoup(html, "html.parser")
    assert bool(soup.select('[data-privacy="confidential"]')) is (compute is True and e2ee is True)
    assert bool(soup.select('[data-privacy="zdr"]')) is (zdr is True)


def test_verified_provider_routes_are_visible_before_overflow() -> None:
    base = replace(next(iter(MODEL_ENDPOINTS.values())), model_id="test/model", usage_type="Credits")
    routes = [replace(base, provider=slug) for slug in ["novita", "openai", "tinfoil"]]
    views = _endpoint_provider_views(routes, fallback_provider="novita")
    assert views[0]["slug"] == "tinfoil"
    assert views[0]["confidential_available"] is True
