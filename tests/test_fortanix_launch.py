import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException
from PIL import Image

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
    endpoint_e2ee,
    endpoint_zero_data_retention,
    endpoints_for_model,
)
from trusted_router.config import Settings
from trusted_router.dashboard import (
    STATIC_DIR,
    _credits_endpoints,
    _short_dollars,
    docs_llms_full_txt,
    llms_txt,
    public_model_detail_html,
)
from trusted_router.fortanix_launch import (
    COMPARISON_MODEL_ID,
    CONTEXT_LENGTH,
    MODEL_ID,
    NATIVE_ID,
    RETAIL_MICRO_PER_M,
    fortanix_launch_offer,
    launch_offer_facts,
    launch_tariff_matches,
)
from trusted_router.internal_model_access import require_internal_model_key
from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS
from trusted_router.provider_locations import (
    PROVIDER_INFERENCE_LOCATIONS,
    provider_inference_locations,
)
from trusted_router.provider_og import provider_og_facts
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars
from trusted_router.routing import candidates_fitting_context, chat_route_endpoint_candidates
from trusted_router.storage import STORE


def test_public_route_is_exact_zero_supplier_cost_at_existing_customer_floor():
    model = MODELS[MODEL_ID]
    routes = endpoints_for_model(MODEL_ID)
    assert len(routes) == 1
    assert launch_tariff_matches(model, routes[0])
    assert RETAIL_MICRO_PER_M == 10_000
    assert routes[0].upstream_id == NATIVE_ID
    assert not model.internal_only and not model.byok_available
    require_internal_model_key({"model": MODEL_ID}, None, Settings(environment="test"))
    assert MODELS["trustedrouter/fortanix-test"].internal_only
    facts = provider_og_facts("fortanix")
    assert facts.model_count == facts.route_count == 1


def test_managed_zdr_does_not_imply_byok_confidentiality_or_attested_location():
    route = endpoints_for_model(MODEL_ID)[0]
    assert endpoint_zero_data_retention(route)
    assert not endpoint_e2ee(route)
    assert not endpoint_zero_data_retention(replace(route, usage_type="BYOK"))
    assert not PROVIDERS["fortanix"].provider_confidential_compute
    assert PROVIDERS["fortanix"].prepaid_zero_data_retention_effective_on == "2026-11-10"
    location = provider_inference_locations("fortanix")
    assert location.locations == ("Santa Clara, California, United States",)
    assert "not hardware-attested" in location.declaration
    assert "failover locations have not been verified" in location.routing
    assert PROVIDERS["fortanix"].provider_headquarters_country is None


def test_public_route_supports_explicit_fortanix_and_zdr_filters():
    routes = chat_route_endpoint_candidates(
        {
            "model": MODEL_ID,
            "provider": {"only": ["fortanix"], "allow_fallbacks": False, "zdr": True},
        },
        Settings(environment="test"),
    )
    assert [(m.id, e.provider, e.upstream_id) for m, e in routes] == [
        (MODEL_ID, "fortanix", NATIVE_ID)
    ]


def test_token_limit_capability_matches_parameter_aware_routing():
    routes = chat_route_endpoint_candidates(
        {
            "model": MODEL_ID,
            "max_tokens": 256,
            "requested_parameters": ["max_tokens"],
            "provider": {
                "only": ["fortanix"], "allow_fallbacks": False, "require_parameters": True,
            },
        },
        Settings(environment="test"),
    )
    assert len(routes) == 1
    model, endpoint = routes[0]
    assert model.id == MODEL_ID
    assert model.supported_parameters == endpoint.supported_parameters == ("max_tokens",)


def test_public_context_uses_verified_provider_limit_without_changing_private_model():
    model = MODELS[MODEL_ID]
    assert CONTEXT_LENGTH == model.context_length == 1_048_576
    assert endpoints_for_model(MODEL_ID)[0].effective_context_length(model) == 1_048_576
    private = MODELS["trustedrouter/fortanix-test"]
    assert private.internal_only and private.context_length == 131_072


@pytest.mark.parametrize("prompt_tokens", [131_073, 1_047_000, 1_048_320])
def test_public_context_admits_large_prompts_including_output_budget(prompt_tokens):
    routes = chat_route_endpoint_candidates(
        {"model": MODEL_ID, "provider": {"only": ["fortanix"], "allow_fallbacks": False}},
        Settings(environment="test"),
    )
    assert candidates_fitting_context(
        routes, prompt_tokens=prompt_tokens, completion_tokens=256,
    ) == routes


def test_public_context_still_rejects_one_token_over_total_window():
    routes = [(MODELS[MODEL_ID], endpoints_for_model(MODEL_ID)[0])]
    with pytest.raises(HTTPException) as raised:
        candidates_fitting_context(routes, prompt_tokens=1_048_321, completion_tokens=256)
    assert raised.value.status_code == 400
    assert raised.value.detail["error"]["type"] == "context_length_exceeded"
    assert "1048576" in raised.value.detail["error"]["message"]


def test_public_context_is_consistent_in_catalog_endpoints_and_model_page(client):
    models = client.get("/v1/models").json()["data"]
    model = next(row for row in models if row["id"] == MODEL_ID)
    assert model["context_length"] == model["top_provider"]["context_length"] == 1_048_576
    routes = client.get(f"/v1/models/{MODEL_ID}/endpoints").json()["data"]
    assert len(routes) == 1 and routes[0]["context_length"] == 1_048_576
    response = client.get(f"/models/{MODEL_ID}")
    assert response.status_code == 200
    assert "1,048,576" in BeautifulSoup(response.text, "html.parser").get_text()


@pytest.mark.parametrize("provider", [{"usage": "byok"}, {"min_privacy": "confidential"}])
def test_unsupported_public_modes_remain_closed(provider):
    with pytest.raises(HTTPException):
        chat_route_endpoint_candidates(
            {"model": MODEL_ID, "provider": provider}, Settings(environment="test")
        )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "cost"),
    [(0, 0, 0), (1, 1, 1), (50, 50, 2), (1_000_000, 1_000_000, 20_000)],
)
def test_minimum_rate_preserves_component_rounding_and_request_floor(
    input_tokens, output_tokens, cost
):
    assert (
        _endpoint_cost_microdollars(endpoints_for_model(MODEL_ID)[0], input_tokens, output_tokens)
        == cost
    )


def test_customer_authorization_locks_introductory_rate_and_bills_once(
    client, user_headers, monkeypatch
):
    created = client.post(
        "/v1/keys", headers=user_headers, json={"name": "Fortanix launch fixture"}
    ).json()
    workspace_id = created["data"]["workspace_id"]
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": created["data"]["hash"],
            "model": MODEL_ID,
            "estimated_input_tokens": 1000,
            "max_output_tokens": 1000,
            "provider": {"only": ["fortanix"], "allow_fallbacks": False},
        },
    )
    assert response.status_code == 200, response.text
    auth = response.json()["data"]
    assert auth["provider"] == "fortanix" and auth["upstream_model"] == NATIVE_ID
    # Later repricing cannot change a request's already-authorized tariff.
    route = endpoints_for_model(MODEL_ID)[0]
    monkeypatch.setitem(
        MODEL_ENDPOINTS,
        route.id,
        replace(route, prompt_price_microdollars_per_million_tokens=900_000),
    )
    before = STORE.credit_money[workspace_id].total_usage_microdollars
    body = {
        "authorization_id": auth["authorization_id"],
        "actual_input_tokens": 1000,
        "actual_output_tokens": 1000,
        "request_id": "fortanix-launch-fixture",
        "finish_reason": "stop",
    }
    settled = client.post("/v1/internal/gateway/settle", json=body)
    assert settled.status_code == 200, settled.text
    money = STORE.credit_money[workspace_id]
    assert money.total_usage_microdollars - before == 20
    assert money.reserved_microdollars == 0
    repeated = client.post("/v1/internal/gateway/settle", json=body)
    assert repeated.status_code == 200 and repeated.json()["data"]["already_settled"]
    assert STORE.credit_money[workspace_id].total_usage_microdollars - before == 20


@pytest.mark.parametrize("path", ["/models"])
def test_public_pages_show_honest_offer_and_keep_legacy_id_private(client, path):
    response = client.get(path)
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    banner = page.select_one("[data-fortanix-promo]")
    assert banner is not None
    assert banner.select_one("a")["href"] == f"/models/{MODEL_ID}"
    assert "$0.01 / 1M input tokens" in banner.get_text()
    assert "$0.01 / 1M output tokens" in banner.get_text()
    assert "No end date announced" in banner.get_text()
    assert "$0.000001 request minimum" in banner.get_text()
    assert "Managed-route ZDR" in banner.get_text()
    assert "Santa Clara inference" in banner.get_text()
    assert "trustedrouter/fortanix-test" not in response.text


@pytest.mark.parametrize(
    "path",
    ["/", "/status", "/trust", "/vibe-coders", "/providers/fortanix", f"/models/{MODEL_ID}"],
)
def test_promotion_preserves_focused_page_layouts(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "data-fortanix-promo" not in response.text
    assert "trustedrouter/fortanix-test" not in response.text


def _other_credit_routes():
    """Credits routes of every other public GLM 5.3 Flash model, as the offer compares."""
    return [
        route
        for model in MODELS.values()
        if model.id != MODEL_ID
        and not model.internal_only
        and model.id.casefold().endswith("/glm-5.3-flash")
        for route in endpoints_for_model(model.id)
        if route.usage_type == "Credits"
    ]


def test_model_page_shows_launch_offer_lede_from_live_prices(client):
    page = BeautifulSoup(client.get(f"/models/{MODEL_ID}").text, "html.parser")
    hero = page.select_one("[data-launch-offer]")
    assert hero is not None
    assert hero.select_one(".md-offer-tag").get_text(strip=True) == "Launch offer"
    lead = hero.select_one("[data-launch-offer-lead]")
    assert lead.get_text(" ", strip=True) == (
        "The lowest-price way to run GLM 5.3 Flash: basically free, at 1\u00a2 per million tokens."
    )
    assert [node.get_text(strip=True) for node in hero.select(".md-stat-value.md-mint")] == [
        "1\u00a2",
        "1\u00a2",
    ]
    others = _other_credit_routes()
    low_in = min(route.prompt_price_microdollars_per_million_tokens for route in others)
    low_out = min(route.completion_price_microdollars_per_million_tokens for route in others)
    compare = " ".join(hero.select_one("[data-launch-offer-compare]").get_text().split())
    assert compare == (
        f"Other GLM 5.3 Flash routes start at {_short_dollars(low_in, digits=2)} in \u00b7 "
        f"{_short_dollars(low_out, digits=2)} out. Intro price; it may change."
    )


def test_launch_offer_lede_follows_repriced_comparison_routes(client, monkeypatch):
    for route in _other_credit_routes():
        monkeypatch.setitem(
            MODEL_ENDPOINTS,
            route.id,
            replace(
                route,
                prompt_price_microdollars_per_million_tokens=80_000,
                completion_price_microdollars_per_million_tokens=300_000,
            ),
        )
    hero = BeautifulSoup(client.get(f"/models/{MODEL_ID}").text, "html.parser")
    compare = hero.select_one("[data-launch-offer-compare]").get_text()
    assert "start at $0.08 in \u00b7 $0.30 out." in " ".join(compare.split())


def test_lowest_price_claim_needs_a_cheaper_route_than_every_alternative(client, monkeypatch):
    route = _other_credit_routes()[0]
    monkeypatch.setitem(
        MODEL_ENDPOINTS,
        route.id,
        replace(route, prompt_price_microdollars_per_million_tokens=RETAIL_MICRO_PER_M),
    )
    offer = launch_offer_facts()
    assert offer is not None and not offer.lowest
    page = BeautifulSoup(client.get(f"/models/{MODEL_ID}").text, "html.parser")
    assert page.select_one("[data-launch-offer]") is not None
    assert page.select_one("[data-launch-offer-lead]") is None
    assert "lowest-price" not in page.text


def test_launch_offer_lede_disappears_with_the_offer(client, monkeypatch):
    route = endpoints_for_model(MODEL_ID)[0]
    monkeypatch.setitem(
        MODEL_ENDPOINTS,
        route.id,
        replace(route, completion_price_microdollars_per_million_tokens=20_000),
    )
    assert launch_offer_facts() is None
    text = client.get(f"/models/{MODEL_ID}").text
    assert "data-launch-offer" not in text
    assert "Launch offer" not in text
    assert "\u00a2" not in text


def test_launch_offer_lede_stays_on_its_own_model_page(client):
    text = client.get(f"/models/{COMPARISON_MODEL_ID}").text
    assert "data-launch-offer" not in text


def test_public_api_lists_exact_customer_prices_and_one_managed_route(client):
    models = client.get("/v1/models").json()["data"]
    model = next(row for row in models if row["id"] == MODEL_ID)
    assert float(model["pricing"]["prompt"]) == float(model["pricing"]["completion"]) == 0.00000001
    assert not any(row["id"] == "trustedrouter/fortanix-test" for row in models)
    assert client.get(f"/v1/models/{MODEL_ID}/endpoints").status_code == 200


@pytest.mark.parametrize(
    "change",
    ["removed", "repriced", "held", "internal", "extra_route", "expired", "retention", "location"],
)
def test_banner_disappears_when_offer_no_longer_matches(monkeypatch, change):
    model = MODELS[MODEL_ID]
    route = endpoints_for_model(MODEL_ID)[0]
    if change == "removed":
        monkeypatch.delitem(MODELS, MODEL_ID)
    elif change == "repriced":
        monkeypatch.setitem(
            MODEL_ENDPOINTS,
            route.id,
            replace(route, completion_price_microdollars_per_million_tokens=20_000),
        )
    elif change == "held":
        monkeypatch.setitem(PREPAID_PROVIDER_HOLD_REASONS, "fortanix", "operator-hold")
    elif change == "internal":
        monkeypatch.setitem(MODELS, MODEL_ID, replace(model, internal_only=True))
    elif change == "extra_route":
        monkeypatch.setitem(MODEL_ENDPOINTS, "unexpected", replace(route, id="unexpected"))
    elif change == "retention":
        monkeypatch.setitem(
            PROVIDERS, "fortanix", replace(PROVIDERS["fortanix"], prepaid_zero_data_retention=False)
        )
    elif change == "location":
        monkeypatch.setitem(
            PROVIDER_INFERENCE_LOCATIONS,
            "fortanix",
            replace(PROVIDER_INFERENCE_LOCATIONS["fortanix"], locations=()),
        )
    else:
        monkeypatch.setitem(
            MODEL_ENDPOINTS,
            route.id,
            replace(route, catalog_valid_until=datetime.min.replace(tzinfo=UTC)),
        )
    assert not fortanix_launch_offer()


@pytest.mark.parametrize("mutation", ["model", "endpoint", "fee", "tier", "extra"])
def test_exact_tariff_coverage_never_exempts_unapproved_public_prices(monkeypatch, mutation):
    from scripts.check_price_coverage import audit

    route = endpoints_for_model(MODEL_ID)[0]
    if mutation == "model":
        monkeypatch.setitem(
            MODELS,
            MODEL_ID,
            replace(MODELS[MODEL_ID], completion_price_microdollars_per_million_tokens=999),
        )
    elif mutation == "endpoint":
        monkeypatch.setitem(
            MODEL_ENDPOINTS, route.id, replace(route, upstream_id="unreviewed-model")
        )
    elif mutation == "fee":
        monkeypatch.setitem(MODEL_ENDPOINTS, route.id, replace(route, request_price_microdollars=1))
    elif mutation == "tier":
        monkeypatch.setitem(MODEL_ENDPOINTS, route.id, replace(route, price_tiers=()))
    else:
        monkeypatch.setitem(
            MODEL_ENDPOINTS,
            "unreviewed",
            replace(route, id="unreviewed", model_id="unreviewed/model"),
        )
    _, _, failures = audit(
        max_age_days=14, now=datetime(2026, 11, 10, tzinfo=UTC), check_model_discovery=False
    )
    assert any("fortanix: public routes differ" in failure for failure in failures)


def _model_page_seo(model_id):
    soup = BeautifulSoup(
        public_model_detail_html(Settings(environment="test"), model_id), "html.parser"
    )
    faq_json: list[tuple[str, str]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        data = json.loads(script.string)
        for node in data.get("@graph", [data]):
            if node.get("@type") == "FAQPage":
                faq_json = [
                    (item["name"], item["acceptedAnswer"]["text"]) for item in node["mainEntity"]
                ]
    faq_visible = [
        (details.summary.get_text(" ", strip=True), details.p.get_text())
        for details in soup.select(".md-faq-list details")
    ]
    return {
        "title": soup.title.string,
        "description": soup.find("meta", attrs={"name": "description"})["content"],
        "og_title": soup.find("meta", property="og:title")["content"],
        "og_description": soup.find("meta", property="og:description")["content"],
        "og_image": soup.find("meta", property="og:image")["content"],
        "twitter_image": soup.find("meta", attrs={"name": "twitter:image"})["content"],
        "og_image_alt": soup.find("meta", property="og:image:alt")["content"],
        "faq_json": dict(faq_json),
        "faq_visible": dict(faq_visible),
    }


def _llms_texts():
    settings = Settings(environment="test")
    return llms_txt(settings), docs_llms_full_txt(settings)


def test_offer_seo_copy_uses_live_route_price_while_gate_is_on():
    offer = launch_offer_facts()
    assert offer is not None and offer.lowest
    assert offer.price_label == "1¢ per million tokens"
    page = _model_page_seo(MODEL_ID)
    assert page["title"] == page["og_title"] == "GLM 5.3 Flash for 1¢ per million tokens (Fortanix)"
    assert page["description"] == page["og_description"] == (
        "GLM 5.3 Flash on Fortanix: 1¢ per million tokens in and out, the lowest GLM 5.3 "
        "Flash price on TrustedRouter. Zero data retention. Intro price, may change."
    )
    assert len(page["description"]) <= 155

    base = _model_page_seo("z-ai/glm-5.3-flash")
    assert base["faq_json"] == base["faq_visible"]
    url = f"https://trustedrouter.com/models/{MODEL_ID}"
    providers = base["faq_json"]["Which providers serve GLM 5.3 Flash?"]
    cost = base["faq_json"]["How much does GLM 5.3 Flash cost through TrustedRouter?"]
    assert providers.endswith(
        "Fortanix also serves GLM 5.3 Flash under the separate model ID "
        f"fortanix/glm-5.3-flash at an introductory 1¢ per million tokens: {url}"
    )
    assert cost.endswith(
        "The separate Fortanix route, fortanix/glm-5.3-flash, costs 1¢ per million tokens "
        "in and out, the lowest GLM 5.3 Flash price on TrustedRouter, as an introductory "
        f"price that may change: {url}"
    )
    llms, full = _llms_texts()
    assert (
        "- Introductory offer: fortanix/glm-5.3-flash (GLM 5.3 Flash on Fortanix, zero data "
        "retention) costs 1¢ per million tokens in and out; pricing may change. "
        f"[Model page]({url})"
    ) in llms.splitlines()
    assert f"url={url}; offer=introductory price, may change" in full
    for text in (providers, cost, page["description"], page["title"]):
        assert "free" not in text.casefold()


def test_offer_page_uses_its_own_social_card_while_gate_is_on():
    card = "https://trustedrouter.com/static/og/glm-5.3-flash-fortanix.png"
    page = _model_page_seo(MODEL_ID)
    assert page["og_image"] == page["twitter_image"] == card
    assert page["og_image_alt"] == (
        "GLM 5.3 Flash, basically free: 1¢ per million tokens on Fortanix"
    )
    assert _model_page_seo("z-ai/glm-5.3-flash")["og_image"] == "https://trustedrouter.com/og.png"
    with Image.open(STATIC_DIR / "og" / "glm-5.3-flash-fortanix.png") as image:
        assert image.size == (1200, 630)


def test_offer_seo_drops_lowest_claim_when_another_glm_flash_route_is_cheaper(monkeypatch):
    route = _credits_endpoints(endpoints_for_model("z-ai/glm-5.3-flash"))[0]
    monkeypatch.setitem(
        MODEL_ENDPOINTS,
        route.id,
        replace(route, prompt_price_microdollars_per_million_tokens=5_000),
    )
    page = _model_page_seo(MODEL_ID)
    assert "lowest" not in page["description"]
    cost = _model_page_seo("z-ai/glm-5.3-flash")["faq_json"][
        "How much does GLM 5.3 Flash cost through TrustedRouter?"
    ]
    assert "fortanix/glm-5.3-flash, costs 1¢ per million tokens in and out as an" in cost
    assert "lowest GLM 5.3 Flash price" not in cost


def test_offer_seo_reverts_when_gate_turns_off(monkeypatch):
    route = endpoints_for_model(MODEL_ID)[0]
    monkeypatch.setitem(
        MODEL_ENDPOINTS,
        route.id,
        replace(route, completion_price_microdollars_per_million_tokens=20_000),
    )
    assert not fortanix_launch_offer()
    assert launch_offer_facts() is None
    page = _model_page_seo(MODEL_ID)
    assert page["title"] == "GLM 5.3 Flash (Fortanix) API, Pricing and Providers"
    assert page["description"].startswith(
        "Compare every TrustedRouter route for GLM 5.3 Flash (Fortanix), including"
    )
    base = _model_page_seo("z-ai/glm-5.3-flash")
    assert base["faq_json"] == base["faq_visible"]
    assert not any("fortanix" in answer.casefold() for answer in base["faq_json"].values())
    llms, full = _llms_texts()
    assert page["og_image"] == "https://trustedrouter.com/og.png"
    assert "Introductory offer" not in llms and MODEL_ID not in llms
    assert "offer=introductory" not in full


def test_pricing_page_leads_its_hero_facts_with_the_live_offer_price(client):
    page = BeautifulSoup(client.get("/pricing").text, "html.parser")
    rows = page.select(".public-hero-metrics > div")
    assert rows[0].has_attr("data-launch-offer-metric")
    assert rows[0].strong.get_text() == "1¢"
    assert rows[0].a["href"] == f"/models/{MODEL_ID}"
    assert rows[1].strong.get_text() == "One"


def test_offer_metric_stays_on_the_pricing_page(client):
    assert "data-launch-offer-metric" not in client.get("/docs").text


def test_pricing_offer_metric_disappears_with_the_offer(client, monkeypatch):
    route = endpoints_for_model(MODEL_ID)[0]
    monkeypatch.setitem(
        MODEL_ENDPOINTS,
        route.id,
        replace(route, completion_price_microdollars_per_million_tokens=20_000),
    )
    assert "data-launch-offer-metric" not in client.get("/pricing").text
