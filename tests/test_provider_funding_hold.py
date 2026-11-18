"""An upstream provider's out-of-funds 402 on a TrustedRouter-funded route.

Incident 2026-11-18: TrustedRouter's Venice account ran dry and Venice-only
models answered customers with Venice's own "add credits" 402 for hours. The
account is TrustedRouter's, so its prepaid routes leave the front of rotation
on the first funding failure and return on the next served request.
"""

from __future__ import annotations

import pytest

from tests.fixture_routes import drop_routes, serve_on_fixture_route
from tests.test_gateway_fallback_billing import _client_and_key
from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.routes.internal import gateway
from trusted_router.routing_state import PROVIDER_FUNDING_HOLD_SECONDS, RoutingState

_MODEL = "venice/funding-hold-fixture"
_VENICE_ONLY = "venice/venice-only-fixture"


def _endpoint(provider: str, usage_type: str = "Credits") -> ModelEndpoint:
    suffix = "byok" if usage_type == "BYOK" else "prepaid"
    return ModelEndpoint(
        id=f"{_MODEL}@{provider}/{suffix}", model_id=_MODEL, provider=provider,
        usage_type=usage_type,
    )


def _model() -> Model:
    return Model(id=_MODEL, name=_MODEL, provider="fixture", context_length=8192)


# Unit: the routing hold itself.


def test_funding_hold_demotes_only_the_held_providers_prepaid_routes() -> None:
    state = RoutingState()
    venice, venice_byok, other = (
        _endpoint("venice"), _endpoint("venice", "BYOK"), _endpoint("together"),
    )
    candidates = [(_model(), venice), (_model(), venice_byok), (_model(), other)]

    assert state.demote_funding_held(candidates, now=0.0) == candidates
    state.hold_provider_funding("venice", now=0.0)

    demoted = state.demote_funding_held(candidates, now=1.0)
    # BYOK runs on the customer's own Venice account and keeps its place; the
    # held prepaid route is kept as the last resort, never dropped.
    assert [endpoint for _, endpoint in demoted] == [venice_byok, other, venice]
    assert state.provider_funding_held("venice", now=1.0)
    assert not state.provider_funding_held("together", now=1.0)


def test_funding_hold_lapses_and_is_released_by_a_served_request() -> None:
    state = RoutingState()
    state.hold_provider_funding("venice", now=0.0)
    assert state.provider_funding_held("venice", now=PROVIDER_FUNDING_HOLD_SECONDS - 1)
    assert not state.provider_funding_held("venice", now=PROVIDER_FUNDING_HOLD_SECONDS)

    state.hold_provider_funding("venice", now=0.0)
    state.release_provider_funding("venice")
    assert not state.provider_funding_held("venice", now=1.0)


def test_route_failure_class_funding_tokens() -> None:
    assert gateway._route_failure_is_funding("upstream_4xx;http=402")
    assert gateway._route_failure_is_funding("upstream_4xx;http=400;funding")
    assert not gateway._route_failure_is_funding("upstream_4xx;http=400;code=invalid_request_error")
    assert not gateway._route_failure_is_funding("upstream_5xx;http=502")
    assert not gateway._route_failure_is_funding("upstream_4xx;http=4021")


# Functional: authorize -> refund/settle -> authorize through the gateway API.


@pytest.fixture
def two_prepaid_hosts(monkeypatch: pytest.MonkeyPatch) -> tuple[ModelEndpoint, ModelEndpoint]:
    drop_routes(monkeypatch, _MODEL)
    venice = serve_on_fixture_route(monkeypatch, _MODEL, "venice", author="venice")
    other = serve_on_fixture_route(monkeypatch, _MODEL, "together", author="venice")
    return venice, other


def _authorize(client, key, model: str = _MODEL, **overrides):
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"], "model": model, "route_type": "chat.completions",
            "estimated_input_tokens": 10, "max_output_tokens": 10, **overrides,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _first_with_provider(client, key, provider: str):
    # Put the named host first explicitly, so the test does not depend on the
    # default ranking between the two fixture hosts.
    data = _authorize(client, key, provider={"order": [provider]})
    assert data["provider"] == provider
    return data


def _only_venice(client, key):
    # One prepaid candidate, as for the incident's Venice-only models.
    data = _authorize(client, key, provider={"order": ["venice"], "allow_fallbacks": False})
    assert _candidate_providers(data) == ["venice"]
    return data


def _candidate_providers(data) -> list[str]:
    return [route["provider"] for route in data["route_candidates"]]


def test_prepaid_402_refund_moves_the_provider_behind_other_hosts(two_prepaid_hosts) -> None:
    venice, other = two_prepaid_hosts
    client, key = _client_and_key()
    data = _only_venice(client, key)

    response = client.post("/v1/internal/gateway/refund", json={
        "authorization_id": data["authorization_id"],
        "selected_endpoint": venice.id,
        "error_status": 402,
        "error_type": "provider_error",
    })
    assert response.status_code == 200, response.text
    assert gateway.ROUTING_STATE.provider_funding_held("venice")

    # Even an explicit preference for the dry host no longer puts it first;
    # it stays authorized as the last resort.
    after = _authorize(client, key, provider={"order": ["venice"]})
    assert after["provider"] == "together"
    assert _candidate_providers(after)[-1] == "venice"

    # A served prepaid request on the provider (the account was refilled)
    # returns it to rotation at once.
    response = client.post("/v1/internal/gateway/settle", json={
        "authorization_id": after["authorization_id"],
        "selected_endpoint": venice.id,
        "actual_input_tokens": 10, "actual_output_tokens": 10,
        "route_type": "chat.completions",
    })
    assert response.status_code == 200, response.text
    assert not gateway.ROUTING_STATE.provider_funding_held("venice")
    assert _authorize(client, key, provider={"order": ["venice"]})["provider"] == "venice"


def test_gateway_reported_provider_funding_type_arms_the_hold(two_prepaid_hosts) -> None:
    # An updated gateway hides the provider's text and reports a neutral 503
    # with error_type provider_funding; that still says out of funds.
    venice, _ = two_prepaid_hosts
    client, key = _client_and_key()
    data = _only_venice(client, key)
    response = client.post("/v1/internal/gateway/refund", json={
        "authorization_id": data["authorization_id"],
        "selected_endpoint": venice.id,
        "error_status": 503,
        "error_type": "provider_funding",
    })
    assert response.status_code == 200, response.text
    assert gateway.ROUTING_STATE.provider_funding_held("venice")


def test_ordinary_provider_failure_does_not_arm_the_hold(two_prepaid_hosts) -> None:
    venice, _ = two_prepaid_hosts
    client, key = _client_and_key()
    data = _only_venice(client, key)
    response = client.post("/v1/internal/gateway/refund", json={
        "authorization_id": data["authorization_id"],
        "selected_endpoint": venice.id,
        "error_status": 503,
        "error_type": "provider_error",
    })
    assert response.status_code == 200, response.text
    assert not gateway.ROUTING_STATE.provider_funding_held("venice")
    assert _authorize(client, key, provider={"order": ["venice"]})["provider"] == "venice"


def test_refund_with_several_prepaid_providers_names_no_dry_provider(two_prepaid_hosts) -> None:
    # A gateway refund names the primary route, whichever candidate's error it
    # carries: with two prepaid providers tried it cannot say which ran dry,
    # so it must not demote the primary.
    _, other = two_prepaid_hosts
    client, key = _client_and_key()
    data = _first_with_provider(client, key, "together")
    assert set(_candidate_providers(data)) == {"together", "venice"}
    response = client.post("/v1/internal/gateway/refund", json={
        "authorization_id": data["authorization_id"],
        "selected_endpoint": other.id,
        "error_status": 402,
        "error_type": "provider_error",
    })
    assert response.status_code == 200, response.text
    assert not gateway.ROUTING_STATE.provider_funding_held("together")
    assert not gateway.ROUTING_STATE.provider_funding_held("venice")


def test_fallback_away_from_a_dry_host_arms_the_hold(two_prepaid_hosts) -> None:
    venice, other = two_prepaid_hosts
    client, key = _client_and_key()
    data = _first_with_provider(client, key, "venice")
    response = client.post("/v1/internal/gateway/settle", json={
        "authorization_id": data["authorization_id"],
        "selected_endpoint": other.id,
        "actual_input_tokens": 10, "actual_output_tokens": 10,
        "route_type": "chat.completions",
        "route_fallbacks": 1,
        "route_failures": [f"venice|{venice.id}|upstream_4xx;http=402;funding"],
    })
    assert response.status_code == 200, response.text
    assert gateway.ROUTING_STATE.provider_funding_held("venice")
    assert not gateway.ROUTING_STATE.provider_funding_held("together")


def test_route_failures_for_routes_the_authorization_never_held_are_ignored(
    two_prepaid_hosts, monkeypatch: pytest.MonkeyPatch,
) -> None:
    venice, other = two_prepaid_hosts
    stranger = serve_on_fixture_route(
        monkeypatch, _VENICE_ONLY, "deepinfra", author="venice",
    )
    client, key = _client_and_key()
    data = _first_with_provider(client, key, "venice")
    response = client.post("/v1/internal/gateway/settle", json={
        "authorization_id": data["authorization_id"],
        "selected_endpoint": venice.id,
        "actual_input_tokens": 10, "actual_output_tokens": 10,
        "route_type": "chat.completions",
        "route_fallbacks": 1,
        "route_failures": [f"deepinfra|{stranger.id}|upstream_4xx;http=402;funding"],
    })
    assert response.status_code == 200, response.text
    assert not gateway.ROUTING_STATE.provider_funding_held("deepinfra")


def test_byok_402_is_the_customers_account_and_never_holds_prepaid_routes(
    two_prepaid_hosts,
) -> None:
    venice, _ = two_prepaid_hosts
    client, key = _client_and_key()
    data = _first_with_provider(client, key, "venice")
    response = client.post("/v1/internal/gateway/settle", json={
        "authorization_id": data["authorization_id"],
        "selected_endpoint": venice.id,
        "actual_input_tokens": 10, "actual_output_tokens": 10,
        "route_type": "chat.completions",
        "route_fallbacks": 1,
        "route_failures": [f"venice|{_MODEL}@venice/byok|upstream_4xx;http=402"],
    })
    assert response.status_code == 200, response.text
    assert not gateway.ROUTING_STATE.provider_funding_held("venice")


def test_a_venice_only_model_keeps_its_held_route_as_the_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drop_routes(monkeypatch, _VENICE_ONLY)
    only = serve_on_fixture_route(monkeypatch, _VENICE_ONLY, "venice", author="venice")
    client, key = _client_and_key()
    gateway.ROUTING_STATE.hold_provider_funding("venice")
    data = _authorize(client, key, model=_VENICE_ONLY)
    # Still authorized: the gateway tries it and, if the account is still dry,
    # answers with a neutral provider-unavailable error, not Venice's text.
    assert data["endpoint_id"] == only.id
