"""Tool calling and structured output reach only routes the catalog does not
say lack them, whatever provider.require_parameters says.

A route that ignores `tools` answers in prose instead of a tool call, and one
that ignores `response_format` answers free text instead of the requested
JSON, both without an error. The gateway sends `requested_parameters` with
every chat request but never sets require_parameters, so before this rule a
tool request to trustedrouter/auto could land on any of its routes, including
the ones that declare no tools.

The rule (routing.CAPABILITY_REQUIRED_PARAMETERS, _takes_capability_parameters):

- tools and tool_choice need tool calling (a route's `tools` declaration);
  response_format needs structured output (`response_format` or
  `structured_outputs`);
- a route that declares the capability takes it;
- a route that does not is skipped when another route of its model declares
  tool calling (tool declarations are kept per host), or when a routing pool
  picked a model that declares the capability on no route and not on its row;
- otherwise the model's declaration, or the caller naming the model, stands;
- with no route left, a 400 names the parameter.

Each refusal below is checked with the rule switched off too: the same request
then reaches the route the rule skips (the behaviour on main).
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
from trusted_router.main import create_app
from trusted_router.routing import chat_route_endpoint_candidates

MODEL_A = "fixture/capability-member-a"
MODEL_B = "fixture/capability-member-b"
NO_RULE: dict[str, frozenset[str]] = {}


def _settings() -> Settings:
    return Settings(environment="test", auto_model_order=f"{MODEL_A},{MODEL_B}")


def _routes(body: dict[str, Any]) -> list[tuple[Model, ModelEndpoint]]:
    return list(chat_route_endpoint_candidates(body, _settings()))


def _hosts(body: dict[str, Any]) -> list[tuple[str, str]]:
    return [(model.id, endpoint.provider) for model, endpoint in _routes(body)]


def _without_the_rule(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> list[tuple[str, str]]:
    with monkeypatch.context() as patch:
        patch.setattr(routing, "CAPABILITY_REQUIRED_PARAMETERS", NO_RULE)
        return _hosts(body)


def _refusal(body: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(HTTPException) as raised:
        _routes(body)
    assert raised.value.status_code == 400
    detail = raised.value.detail
    assert isinstance(detail, dict)
    return detail["error"]


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    routes: list[tuple[str, tuple[str, ...]]],
    *,
    model_parameters: tuple[str, ...] = (),
) -> None:
    """Serve a fixture model on (host, supported_parameters) routes; the
    model row itself declares `model_parameters`."""
    model = Model(
        id=model_id, name=model_id, provider="deepinfra", context_length=131_072,
        supported_parameters=model_parameters,
    )
    for host, parameters in routes:
        serve_on_fixture_route(
            monkeypatch, model_id, host, author="deepinfra", model=model,
            supported_parameters=parameters,
        )


def _tools(model: str, **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "route_type": "chat.completions",
        "requested_parameters": ["max_tokens", "tools"],
        **extra,
    }


def test_a_tool_request_skips_a_route_without_tools_when_another_route_has_them(monkeypatch):
    _serve(monkeypatch, MODEL_A, [("deepinfra", ("max_tokens",)), ("novita", ("tools", "max_tokens"))])
    _serve(monkeypatch, MODEL_B, [("parasail", ("tools", "max_tokens"))])
    every_route = {(MODEL_A, "deepinfra"), (MODEL_A, "novita"), (MODEL_B, "parasail")}

    # Positive control: without the rule (main), the tool request reaches A's
    # route that declares no tools, so the next assertion is not vacuous.
    assert set(_without_the_rule(monkeypatch, _tools(AUTO_MODEL_ID))) == every_route

    assert set(_hosts(_tools(AUTO_MODEL_ID))) == {(MODEL_A, "novita"), (MODEL_B, "parasail")}
    # The same holds when the caller names the model.
    assert _hosts(_tools(MODEL_A)) == [(MODEL_A, "novita")]
    # A request without tools still reaches every route.
    assert set(_hosts({"model": AUTO_MODEL_ID, "route_type": "chat.completions"})) == every_route


def test_tool_choice_needs_tool_calling_not_its_own_declaration(monkeypatch):
    # Most tool routes declare `tools` without `tool_choice`; tool_choice is
    # part of tool calling.
    _serve(monkeypatch, MODEL_A, [("deepinfra", ("max_tokens",)), ("novita", ("tools",))])
    body = {"model": MODEL_A, "route_type": "chat.completions", "requested_parameters": ["tool_choice"]}
    assert _without_the_rule(monkeypatch, body) == [(MODEL_A, "deepinfra"), (MODEL_A, "novita")]
    assert _hosts(body) == [(MODEL_A, "novita")]


def test_a_pool_skips_a_model_that_declares_tools_nowhere(monkeypatch):
    _serve(monkeypatch, MODEL_A, [("novita", ("tools",))])
    _serve(monkeypatch, MODEL_B, [("deepinfra", ("max_tokens", "temperature"))])

    assert _without_the_rule(monkeypatch, _tools(AUTO_MODEL_ID)) == [
        (MODEL_A, "novita"), (MODEL_B, "deepinfra"),
    ]
    assert _hosts(_tools(AUTO_MODEL_ID)) == [(MODEL_A, "novita")]
    # A caller who names B vouches for it: the catalog says nothing either
    # way, and some models take tools without declaring them.
    assert _hosts(_tools(MODEL_B)) == [(MODEL_B, "deepinfra")]
    # Naming B next to the pool keeps it too.
    assert _hosts(_tools(AUTO_MODEL_ID, models=[MODEL_B])) == [
        (MODEL_A, "novita"), (MODEL_B, "deepinfra"),
    ]


def test_a_model_declaration_stands_for_routes_that_say_nothing(monkeypatch):
    _serve(monkeypatch, MODEL_A, [("novita", ("tools",))])
    _serve(
        monkeypatch, MODEL_B, [("deepinfra", ("max_tokens",)), ("parasail", ("max_tokens",))],
        model_parameters=("tools", "max_tokens"),
    )
    assert set(_hosts(_tools(AUTO_MODEL_ID))) == {
        (MODEL_A, "novita"), (MODEL_B, "deepinfra"), (MODEL_B, "parasail"),
    }


def test_structured_output_trusts_the_model_not_route_silence(monkeypatch):
    # The makers' own routes (Google, Z.AI, Moonshot, Alibaba, MiniMax)
    # declare no structured output while third-party hosts of the same model
    # do, so route silence is not evidence for it.
    _serve(
        monkeypatch, MODEL_A,
        [("deepinfra", ("max_tokens",)), ("novita", ("structured_outputs",))],
    )
    _serve(monkeypatch, MODEL_B, [("parasail", ("max_tokens", "temperature"))])
    body = {
        "model": AUTO_MODEL_ID,
        "route_type": "responses",
        "requested_parameters": ["response_format"],
    }
    assert _without_the_rule(monkeypatch, body) == [
        (MODEL_A, "deepinfra"), (MODEL_A, "novita"), (MODEL_B, "parasail"),
    ]
    # B declares structured output nowhere, so the pool skips it; both of
    # A's routes stay.
    assert _hosts(body) == [(MODEL_A, "deepinfra"), (MODEL_A, "novita")]
    # A route declaring response_format itself takes it as well.
    _serve(monkeypatch, MODEL_B, [("parasail", ("response_format",))])
    assert (MODEL_B, "parasail") in _hosts(body)


def test_require_parameters_keeps_its_full_set_meaning(monkeypatch):
    _serve(
        monkeypatch, MODEL_A, [("deepinfra", ("max_tokens",))],
        model_parameters=("tools", "max_tokens"),
    )
    assert _hosts(_tools(MODEL_A)) == [(MODEL_A, "deepinfra")]
    # require_parameters still needs the route's own declaration of every
    # parameter sent, so the model's declaration does not satisfy it.
    error = _refusal(_tools(MODEL_A, provider={"require_parameters": True}))
    assert error["message"] == "No route candidates match the requested provider filters"


def test_no_route_left_is_a_400_naming_the_parameter(monkeypatch):
    _serve(monkeypatch, MODEL_A, [("deepinfra", ("max_tokens",)), ("novita", ("tools",))])
    pinned = {"provider": {"only": ["deepinfra"]}}
    # Positive control: the pinned host serves the request without tools.
    assert _hosts({"model": MODEL_A, "route_type": "chat.completions", **pinned}) == [
        (MODEL_A, "deepinfra"),
    ]
    assert _without_the_rule(monkeypatch, _tools(MODEL_A, **pinned)) == [(MODEL_A, "deepinfra")]

    error = _refusal(_tools(MODEL_A, **pinned))
    assert error == {
        "code": 400,
        "message": "No route for the requested model supports the parameter: tools",
        "type": "model_not_supported",
        "source": "router",
        "param": "tools",
        "unsupported_parameters": ["tools"],
    }


def test_parameters_no_single_route_takes_together_are_named_together(monkeypatch):
    _serve(monkeypatch, MODEL_A, [("novita", ("tools",))])
    _serve(monkeypatch, MODEL_B, [("deepinfra", ("structured_outputs",))])
    body = {
        "model": AUTO_MODEL_ID,
        "route_type": "chat.completions",
        "requested_parameters": ["tools", "response_format"],
    }
    assert len(_without_the_rule(monkeypatch, body)) == 2
    error = _refusal(body)
    assert error["message"] == (
        "No route for the requested model supports these parameters together: "
        "response_format, tools"
    )
    assert error["unsupported_parameters"] == ["response_format", "tools"]


@pytest.mark.parametrize("route_type", [None, "chat.completions", "responses"])
def test_the_rule_reads_parameters_the_gateway_derives_from_the_request(monkeypatch, route_type):
    _serve(monkeypatch, MODEL_A, [("deepinfra", ("max_tokens",)), ("novita", ("tools",))])
    body = {"model": MODEL_A, "requested_parameters": ["tools"]}
    if route_type is not None:
        body["route_type"] = route_type
    assert _hosts(body) == [(MODEL_A, "novita")]


@pytest.mark.parametrize(
    "route_type", ["fusion.panel", "fusion.judge", "advisor.worker", "responses.web_search.planner"],
)
def test_orchestration_subcalls_are_not_filtered_by_their_parents_parameters(monkeypatch, route_type):
    # Sub-calls copy their parent's requested_parameters while dropping or
    # replacing its tools and response_format (a Synth judge has no tools and
    # its own json_object format), so the list does not describe them.
    _serve(monkeypatch, MODEL_A, [("deepinfra", ("max_tokens",)), ("novita", ("tools",))])
    body = {"model": MODEL_A, "route_type": route_type, "requested_parameters": ["tools", "response_format"]}
    assert _hosts(body) == [(MODEL_A, "deepinfra"), (MODEL_A, "novita")]


def test_authorize_sends_a_tool_request_only_to_tool_routes(monkeypatch):
    _serve(monkeypatch, MODEL_A, [("deepinfra", ("max_tokens",)), ("novita", ("tools", "max_tokens"))])
    client = TestClient(create_app(_settings(), init_observability=False))
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "capability routing"},
    )
    assert created.status_code == 201, created.text
    key_hash = created.json()["data"]["hash"]

    def authorize(idempotency_key: str, **extra: Any) -> Any:
        return client.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": key_hash,
                "model": MODEL_A,
                "route_type": "chat.completions",
                "estimated_input_tokens": 10,
                "max_output_tokens": 10,
                "idempotency_key": idempotency_key,
                **extra,
            },
        )

    plain = authorize("plain")
    assert plain.status_code == 200, plain.text
    assert {c["provider"] for c in plain.json()["data"]["route_candidates"]} == {"deepinfra", "novita"}

    tools = authorize("tools", requested_parameters=["max_tokens", "tools"])
    assert tools.status_code == 200, tools.text
    assert [c["provider"] for c in tools.json()["data"]["route_candidates"]] == ["novita"]
    assert tools.json()["data"]["provider"] == "novita"

    refused = authorize(
        "pinned", requested_parameters=["tools"], provider={"only": ["deepinfra"]},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["type"] == "model_not_supported"
    assert refused.json()["error"]["message"] == (
        "No route for the requested model supports the parameter: tools"
    )


def test_ordinary_requests_route_exactly_as_before(monkeypatch):
    """Every chat model and routing pool in the catalog: a request without
    tools, tool_choice or response_format gets the same routes in the same
    order with the rule and without it."""
    settings = Settings(environment="test")
    model_ids = sorted(
        model.id
        for model in catalog.MODELS.values()
        if model.supports_chat and not model.internal_only
        and (model.id not in catalog.META_MODEL_IDS or catalog.orchestration_role(model.id) == "routing_pool")
    )
    bodies = (
        {"route_type": "chat.completions"},
        {
            "route_type": "chat.completions",
            "requested_parameters": ["max_tokens", "temperature", "top_p", "seed", "stop", "reasoning"],
        },
    )

    def routes(body: dict[str, Any]) -> list[str] | str:
        try:
            return [endpoint.id for _model, endpoint in chat_route_endpoint_candidates(body, settings)]
        except HTTPException as exc:
            return str(exc.detail)

    with_rule = {
        (model_id, index): routes({"model": model_id, **body})
        for model_id in model_ids for index, body in enumerate(bodies)
    }
    monkeypatch.setattr(routing, "CAPABILITY_REQUIRED_PARAMETERS", NO_RULE)
    without_rule = {
        (model_id, index): routes({"model": model_id, **body})
        for model_id in model_ids for index, body in enumerate(bodies)
    }
    assert len(model_ids) > 100
    assert with_rule == without_rule


@pytest.mark.catalog_as_built
@pytest.mark.parametrize(
    "pool",
    sorted(
        model_id for model_id in catalog.META_MODEL_IDS
        if catalog.orchestration_role(model_id) == "routing_pool"
    ),
)
def test_pool_tool_requests_reach_only_routes_with_tool_evidence(pool):
    try:
        reached = list(chat_route_endpoint_candidates(_tools(pool), Settings(environment="test")))
    except HTTPException:
        return
    for model, endpoint in reached:
        if "tools" in endpoint.supported_parameters or model.id == pool:
            # A pool that routes to itself (polyphemus-1.0) is named, not picked.
            continue
        # A route that says nothing is reached only for a model that declares
        # tools itself while none of its routes does.
        assert "tools" in model.supported_parameters, (pool, endpoint.id)
        assert not any(
            "tools" in route.supported_parameters for route in catalog.endpoints_for_model(model.id)
        ), (pool, endpoint.id)
