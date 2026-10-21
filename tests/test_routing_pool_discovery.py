"""A routing pool publishes only what a request naming it is sure to get.

Routing pools (trustedrouter/auto, cheap, fast, eu, zdr, e2e, green, ...) have
no routes of their own: each request runs on one model from the pool. Their
/v1/models rows used to carry only the base `max_tokens`, so clients that
filter on supported_parameters (charmbracelet/catwalk#659,
electric-capital/quest#93) treated auto as unable to call tools.

The rule, checked here against routing itself rather than against the code
that computes the row:

- a parameter is listed exactly when a request naming the pool with
  provider.require_parameters: true and that parameter keeps every model the
  pool routes to without it, so the pool claims no more than its weakest
  model;
- an input modality is listed exactly when routing has a pool route for a
  request carrying it, and such a request reaches only routes that take it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.fixture_routes import serve_on_fixture_route
from trusted_router import catalog, routing
from trusted_router.catalog_data import AUTO_MODEL_ID, Model, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates

SETTINGS = Settings(environment="test")

ROUTING_POOLS = sorted(
    model_id
    for model_id in catalog.META_MODEL_IDS
    if catalog.orchestration_role(model_id) == "routing_pool"
)

FIXTURE_A = "fixture/pool-member-a"
FIXTURE_B = "fixture/pool-member-b"


def _routes(body: dict[str, Any], settings: Settings = SETTINGS) -> list[tuple[Model, ModelEndpoint]]:
    try:
        return list(chat_route_endpoint_candidates(body, settings))
    except HTTPException:
        return []


def _models(routes: list[tuple[Model, ModelEndpoint]]) -> set[str]:
    return {model.id for model, _endpoint in routes}


def _requiring(pool: str, parameter: str) -> dict[str, Any]:
    return {
        "model": pool,
        "requested_parameters": [parameter],
        "provider": {"require_parameters": True},
    }


def _row(client: TestClient, model_id: str) -> dict[str, Any]:
    response = client.get("/v1/models")
    assert response.status_code == 200
    rows = [row for row in response.json()["data"] if row["id"] == model_id]
    assert len(rows) == 1
    return rows[0]


def test_the_pools_are_the_routing_aliases():
    assert {
        AUTO_MODEL_ID,
        "trustedrouter/cheap",
        "trustedrouter/fast",
        "trustedrouter/eu",
        "trustedrouter/zdr",
        "trustedrouter/e2e",
        "trustedrouter/confidential",
        "trustedrouter/green",
    } <= set(ROUTING_POOLS)
    # Orchestration models run several gateway calls, each with its own
    # parameters; their rows are not derived from their members' routes.
    for orchestration in ("trustedrouter/synth", "trustedrouter/synth-code", "trustedrouter/prometheus"):
        assert orchestration not in ROUTING_POOLS
        assert catalog._routing_pool_discovery(catalog.MODELS[orchestration]) == ((), ())


@pytest.mark.parametrize("pool", ROUTING_POOLS)
def test_pool_lists_a_parameter_exactly_when_routing_keeps_every_model_for_it(pool):
    row = catalog.model_to_openrouter_shape(catalog.MODELS[pool])
    listed = set(row["supported_parameters"])
    pool_routes = _routes({"model": pool})
    if not pool_routes:
        # Nothing to route to: only the base chat contract.
        assert row["supported_parameters"] == ["max_tokens"]
        return
    every_model = _models(pool_routes)
    named = {parameter for _model, endpoint in pool_routes for parameter in endpoint.supported_parameters}
    for parameter in sorted(named | listed):
        kept = _routes(_requiring(pool, parameter))
        assert (_models(kept) == every_model) == (parameter in listed), (pool, parameter)


@pytest.mark.parametrize("pool", ROUTING_POOLS)
def test_pool_lists_an_input_modality_exactly_when_routing_has_a_route_for_it(pool):
    row = catalog.model_to_openrouter_shape(catalog.MODELS[pool])
    listed = row["architecture"]["input_modalities"]
    assert listed[0] == "text"
    for modality in ("image", "audio", "video"):
        reached = _routes({"model": pool, "input_modalities": ["text", modality]})
        assert bool(reached) == (modality in listed), (pool, modality)
        for _model, endpoint in reached:
            assert modality in (endpoint.input_modalities or ()), (pool, endpoint.id)


@pytest.mark.catalog_as_built
def test_auto_declares_tools_and_sends_tool_requests_only_to_tool_models(client):
    row = _row(client, AUTO_MODEL_ID)
    assert "tools" in row["supported_parameters"]
    # Still no capabilities object: the selected model can vary.
    assert "capabilities" not in row["trustedrouter"]

    every_model = _models(_routes({"model": AUTO_MODEL_ID}))
    assert len(every_model) > 1
    # Positive control: with require_parameters, a tool request keeps every
    # model in auto's ladder and runs only on routes that declare tools.
    kept = _routes(_requiring(AUTO_MODEL_ID, "tools"))
    assert _models(kept) == every_model
    assert all("tools" in endpoint.supported_parameters for _model, endpoint in kept)
    # Without it, every model the request can reach still takes tools on
    # one of its routes, and each one's own row says so.
    rows = {model["id"]: model for model in client.get("/v1/models").json()["data"]}
    for model_id in _models(_routes({"model": AUTO_MODEL_ID, "requested_parameters": ["tools"]})):
        assert "tools" in rows[model_id]["supported_parameters"], model_id


def _fixture_pool(monkeypatch, *, a_routes, b_routes) -> Settings:
    """Make auto's ladder two fixture models, each served on the given routes:
    (host, supported_parameters, input_modalities) triples."""
    settings = Settings(environment="test", auto_model_order=f"{FIXTURE_A},{FIXTURE_B}")
    monkeypatch.setattr(routing, "_app_settings", lambda: settings)
    for model_id, routes in ((FIXTURE_A, a_routes), (FIXTURE_B, b_routes)):
        for host, parameters, modalities in routes:
            serve_on_fixture_route(
                monkeypatch,
                model_id,
                host,
                author="deepinfra",
                supported_parameters=parameters,
                input_modalities=modalities,
            )
    return settings


def test_one_model_without_a_tool_route_takes_tools_off_the_pool(client, monkeypatch):
    settings = _fixture_pool(
        monkeypatch,
        a_routes=[("novita", ("tools", "max_tokens"), ("text",))],
        b_routes=[("deepinfra", ("max_tokens", "temperature"), ("text",))],
    )
    assert _models(_routes({"model": AUTO_MODEL_ID}, settings)) == {FIXTURE_A, FIXTURE_B}
    row = _row(client, AUTO_MODEL_ID)
    assert row["supported_parameters"] == ["max_tokens"]
    assert row["architecture"]["input_modalities"] == ["text"]
    # Routing agrees: requiring tools loses model B.
    assert _models(_routes(_requiring(AUTO_MODEL_ID, "tools"), settings)) == {FIXTURE_A}


def test_pool_lists_tools_when_every_model_has_a_tool_route(client, monkeypatch):
    settings = _fixture_pool(
        monkeypatch,
        a_routes=[
            ("novita", ("tools", "max_tokens"), ("text",)),
            ("deepinfra", ("max_tokens",), ("text",)),
        ],
        b_routes=[("deepinfra", ("tools", "max_tokens", "temperature"), ("text", "image"))],
    )
    row = _row(client, AUTO_MODEL_ID)
    assert row["supported_parameters"] == ["tools", "max_tokens"]
    assert row["architecture"]["input_modalities"] == ["text", "image"]
    assert row["architecture"]["modality"] == "text+image->text"

    # A's tool-less route is a candidate for a request without tools. This is
    # what makes the next assertion more than vacuous.
    unfiltered = _routes({"model": AUTO_MODEL_ID}, settings)
    assert any("tools" not in endpoint.supported_parameters for _model, endpoint in unfiltered)
    kept = _routes(_requiring(AUTO_MODEL_ID, "tools"), settings)
    assert _models(kept) == {FIXTURE_A, FIXTURE_B}
    assert all("tools" in endpoint.supported_parameters for _model, endpoint in kept)

    # temperature is on B only, so it is not listed and requiring it loses A.
    assert _models(_routes(_requiring(AUTO_MODEL_ID, "temperature"), settings)) == {FIXTURE_B}

    # Image input is listed because routing sends it only to a route that
    # takes images.
    reached = _routes({"model": AUTO_MODEL_ID, "input_modalities": ["text", "image"]}, settings)
    assert [(model.id, endpoint.provider) for model, endpoint in reached] == [(FIXTURE_B, "deepinfra")]
