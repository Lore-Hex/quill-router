from dataclasses import replace

import pytest
from fastapi import HTTPException

from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.routing import provider_route_preferences


def test_performance_preferences_accept_openrouter_scalar_and_percentiles():
    prefs = provider_route_preferences(
        {
            "provider": {
                "preferred_max_latency": {"p50": 2, "p99": 8},
                "preferred_min_throughput": 30,
            }
        }
    )
    assert prefs.preferred_max_latency == (("p50", 2.0), ("p99", 8.0))
    assert prefs.preferred_min_throughput == (("p50", 30.0),)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "10", {}, {"p95": 3}])
def test_invalid_performance_preferences_fail_closed(value):
    with pytest.raises(HTTPException) as error:
        provider_route_preferences({"provider": {"preferred_max_latency": value}})
    assert error.value.status_code == 400


def candidates():
    model = next(m for m in MODELS.values() if endpoints_for_model(m.id))
    endpoint = endpoints_for_model(model.id)[0]
    return [
        (model, replace(endpoint, id="slow", provider="anthropic")),
        (model, replace(endpoint, id="fast", provider="openai")),
    ]


def test_thresholds_soft_and_region_scoped_with_expiry():
    from trusted_router.routing_state import RoutingState

    state = RoutingState()
    routes = candidates()
    prefs = provider_route_preferences({"provider": {"preferred_max_latency": 2}})
    state.observe("fast", "us", latency=1, throughput=50, now=100)
    state.observe("slow", "us", latency=5, throughput=10, now=100)
    assert state.rank(routes, prefs, region="us", now=101) == routes[::-1]
    assert state.rank(routes, prefs, region="eu", now=101) == routes
    assert state.rank(routes, prefs, region="us", now=401) == routes
    assert len(state.rank(routes, prefs, region="us", now=101)) == 2
    ordered = replace(prefs, order=("anthropic", "openai"))
    assert state.rank(routes, ordered, region="us", now=101) == routes


def test_affinity_is_bounded_isolated_and_never_reintroduces_filtered_routes():
    from trusted_router.routing_state import RoutingState

    state = RoutingState(max_sessions=2)
    routes = candidates()
    prefs = provider_route_preferences({})
    key = ("tenant", "requested-model", "us", "opaque-session")
    state.remember(key, "fast", now=100)
    assert state.rank(routes, prefs, region="us", session=key, now=101) == routes[::-1]
    assert state.rank(routes[:1], prefs, region="us", session=key, now=101) == routes[:1]
    other = ("other-tenant", *key[1:])
    assert state.rank(routes, prefs, region="us", session=other, now=101) == routes
    assert (
        state.rank(routes, replace(prefs, order=("anthropic",)), region="us", session=key, now=101)
        == routes
    )
    assert state.rank(routes, prefs, region="us", session=key, now=701) == routes
    for i in range(10):
        state.remember((str(i), *key[1:]), "fast", now=800)
    assert state.session_count == 2


def test_throughput_percentiles_use_lower_tail_and_require_all_thresholds():
    from trusted_router.routing_state import RoutingState

    state = RoutingState()
    routes = candidates()
    for value in [1] * 20 + [100] * 80:
        state.observe("fast", "us", latency=1, throughput=value, now=100)
    # Keep the whole distribution for this assertion; default sample cap is >= 100.
    prefs = provider_route_preferences(
        {"provider": {"preferred_min_throughput": {"p50": 50, "p90": 50}}}
    )
    assert state.rank(routes, prefs, region="us", now=101) == routes
