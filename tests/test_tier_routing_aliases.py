"""Fixed tier pools share Fast's single-model routing and fallback contract."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fixture_routes import drop_routes, serve_on_fixture_route
from trusted_router.catalog import (
    META_MODEL_IDS,
    MODELS,
    ROUTING_MODEL_MIN_PRIVACY_TIERS,
    TIER_MODEL_ORDERS,
    _meta_route_kind,
    endpoints_for_model,
    meta_candidate_models,
    offers_chat,
    orchestration_primitive,
    orchestration_role,
)
from trusted_router.config import Settings
from trusted_router.routing import _requested_model_ids, chat_route_endpoint_candidates


def _assert_tier_members_exist(alias: str) -> None:
    order = TIER_MODEL_ORDERS[alias]
    assert order, f"{alias} must have members"
    assert len(order) == len(set(order)), f"{alias} has duplicate members"
    for member in order:
        assert member in MODELS, f"Unknown tier member: {member}"
        assert offers_chat(MODELS[member]), member
        assert endpoints_for_model(member), member


def test_tier_alias_ids() -> None:
    assert set(TIER_MODEL_ORDERS) == {
        "trustedrouter/frontier",
        "trustedrouter/advanced",
        "trustedrouter/professional",
        "trustedrouter/efficient",
    }
    assert TIER_MODEL_ORDERS.keys() <= META_MODEL_IDS


@pytest.mark.catalog_as_built
@pytest.mark.parametrize("alias", TIER_MODEL_ORDERS)
def test_tier_members_exist_and_are_routable_chat_models(alias: str) -> None:
    _assert_tier_members_exist(alias)


@pytest.mark.parametrize("alias", TIER_MODEL_ORDERS)
def test_unknown_tier_member_fails_membership_check(
    alias: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = "test/unknown-tier-member"
    assert unknown not in MODELS
    monkeypatch.setitem(TIER_MODEL_ORDERS, alias, (*TIER_MODEL_ORDERS[alias], unknown))
    with pytest.raises(AssertionError, match=f"Unknown tier member: {unknown}"):
        _assert_tier_members_exist(alias)


@pytest.mark.catalog_as_built
@pytest.mark.parametrize("alias", TIER_MODEL_ORDERS)
def test_tier_order_and_metadata(alias: str) -> None:
    order = TIER_MODEL_ORDERS[alias]
    assert tuple(model.id for model in meta_candidate_models(alias)) == order
    assert _meta_route_kind(alias) == "tier_pool"
    assert orchestration_role(alias) == "routing_pool"
    assert orchestration_primitive(alias) is None
    assert alias not in ROUTING_MODEL_MIN_PRIVACY_TIERS
    model = MODELS[alias]
    assert model.name == f"TrustedRouter {alias.split('/', 1)[1].title()}"
    assert model.provider == "trustedrouter"
    assert model.prepaid_available is True
    assert model.byok_available is True
    assert model.context_length == min(MODELS[member].context_length for member in order)
    ids, overrides = _requested_model_ids({"model": alias}, Settings(environment="test"))
    assert ids == list(order)
    assert "privacy_requirements" not in overrides


@pytest.mark.parametrize("alias", TIER_MODEL_ORDERS)
def test_tier_routes_preferred_then_fallback_without_privacy_floor(
    alias: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = TIER_MODEL_ORDERS[alias]
    for member in order:
        drop_routes(monkeypatch, member)
        serve_on_fixture_route(monkeypatch, member, "gmi", author=MODELS[member].provider)
    settings = Settings(environment="test")
    routes = chat_route_endpoint_candidates({"model": alias}, settings)
    assert [model.id for model, _endpoint in routes] == list(order)
    # Removing the preferred member's routes advances to the next member only.
    drop_routes(monkeypatch, order[0])
    fallback = chat_route_endpoint_candidates({"model": alias}, settings)
    assert [model.id for model, _endpoint in fallback] == list(order[1:])


def test_public_catalog_lists_tiers_and_their_order(client: TestClient) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    for alias, order in TIER_MODEL_ORDERS.items():
        row = rows[alias]
        assert row["context_length"] == min(MODELS[member].context_length for member in order)
        assert row["trustedrouter"]["route_kind"] == "tier_pool"
        assert row["trustedrouter"]["auto_candidates"] == list(order)
        assert row["trustedrouter"]["orchestration_role"] == "routing_pool"


def test_choose_catalog_lists_tiers_without_privacy_guarantees(client: TestClient) -> None:
    response = client.get("/choose/catalog.json")
    assert response.status_code == 200
    routes = {route["id"]: route for route in response.json()["routes"]}
    for alias, order in TIER_MODEL_ORDERS.items():
        assert routes[alias]["min_privacy_tier"] == 0
        assert routes[alias]["candidate_count"] == len(order)
        assert "ordered fallback" in routes[alias]["description"]


@pytest.mark.parametrize("path", [
    "/llms.txt", "/docs/llms.txt", "/docs/llms-full.txt",
    "/docs/agent-setup", "/choose", "/for-developers", "/claude-code", "/docs/evals",
])
def test_public_alias_guides_include_tiers(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    for alias in TIER_MODEL_ORDERS:
        assert alias in response.text
