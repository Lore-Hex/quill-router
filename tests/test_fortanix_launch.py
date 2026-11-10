from dataclasses import replace
from datetime import UTC, datetime

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
    endpoint_e2ee,
    endpoint_zero_data_retention,
    endpoints_for_model,
)
from trusted_router.config import Settings
from trusted_router.fortanix_launch import (
    MODEL_ID,
    NATIVE_ID,
    RETAIL_MICRO_PER_M,
    fortanix_launch_offer,
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
from trusted_router.routing import chat_route_endpoint_candidates
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


@pytest.mark.parametrize("path", ["/", "/models"])
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
    "path", ["/status", "/trust", "/vibe-coders", "/providers/fortanix", f"/models/{MODEL_ID}"]
)
def test_promotion_preserves_focused_page_layouts(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "data-fortanix-promo" not in response.text
    assert "trustedrouter/fortanix-test" not in response.text


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
