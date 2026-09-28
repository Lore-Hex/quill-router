"""`trustedrouter/auto` is the route most traffic takes, including every caller
who never chose a model. Two separate things keep that defensible, and they are
easy to confuse:

* the LADDER (`DEFAULT_AUTO_MODEL_ORDER`) is a preference order — cheap and
  privacy-clearing models first;
* the GUARANTEE is enforced in routing — a request carrying a privacy floor or
  a jurisdiction filters candidates BEFORE any provider is contacted, and 400s
  if nothing qualifies.

The guarantee is what makes it safe to keep a non-zero-retention model like
Anthropic in the ladder at all, so it is tested here explicitly rather than
assumed.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, endpoint_privacy_tier
from trusted_router.catalog_data import (
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
    DEFAULT_AUTO_MODEL_ORDER,
    PRIVACY_TIER_ZERO_RETENTION,
    US_FOCUSED_PROVIDER_ORDER,
    Model,
    ModelEndpoint,
)
from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.routing_candidates import auto_candidate_models

# The leading models that can satisfy a US zero-retention request.
QUALIFYING_LEAD_MODELS = 3


def _us_zdr_providers(model_id: str) -> set[str]:
    return {
        endpoint.provider
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == model_id
        and endpoint.provider in US_FOCUSED_PROVIDER_ORDER
        and endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
    }


def test_leading_privacy_compatible_auto_candidates_are_us_and_zero_retention() -> None:
    """After policy filtering, the leading ZDR choices must clear the floor."""
    qualifying = [
        model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if _us_zdr_providers(model_id)
    ][:QUALIFYING_LEAD_MODELS]
    offenders = {
        model_id: sorted(
            {
                (endpoint.provider, endpoint_privacy_tier(endpoint))
                for endpoint in MODEL_ENDPOINTS.values()
                if endpoint.model_id == model_id
            }
        )
        for model_id in qualifying
        if not _us_zdr_providers(model_id)
    }
    assert not offenders, (
        f"the first {QUALIFYING_LEAD_MODELS} compatible auto candidates must have a "
        "US-hosted endpoint at or "
        f"above zero-retention; these do not: {offenders}"
    )


@pytest.mark.provider_health
def test_auto_ladder_spans_more_than_one_provider() -> None:
    """A single-provider ladder makes one provider outage an `auto` outage.

    Live provider state: which hosts serve the leading models today.
    provider-catalog-health.yml reports it hourly, and the price refresh does
    not wait on it."""
    providers: set[str] = set()
    qualifying = [
        model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if _us_zdr_providers(model_id)
    ][:QUALIFYING_LEAD_MODELS]
    for model_id in qualifying:
        providers |= _us_zdr_providers(model_id)
    assert len(providers) > 1, f"the leading auto candidates share one provider: {providers}"


def test_glm53_recommendations_preserve_independent_fallbacks() -> None:
    assert DEFAULT_AUTO_MODEL_ORDER[:6] == [
        "z-ai/glm-5.3-flash",
        "z-ai/glm-5.3",
        "deepseek/deepseek-v4-pro-0813",
        "deepseek/deepseek-v4-flash-0731",
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
    ]


def test_zdr_filter_uses_only_compatible_routes_for_global_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The leader is served on two fixture routes, one of which clears zero
    # retention. Which hosts serve it today is provider state, not this rule.
    leader = DEEPSEEK_V4_PRO_0813_MODEL_ID
    monkeypatch.setitem(
        MODELS,
        leader,
        MODELS.get(leader)
        or Model(id=leader, name=leader, provider="deepseek", context_length=131_072,
                 prepaid_available=True),
    )
    for endpoint_id, endpoint in list(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == leader:
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    routes = {
        host: ModelEndpoint(
            id=f"{leader}@{host}/prepaid", model_id=leader, provider=host,
            usage_type="Credits", upstream_id=f"fixture-{host}",
            prompt_price_microdollars_per_million_tokens=1_000_000,
            completion_price_microdollars_per_million_tokens=3_000_000,
        )
        for host in ("baseten", "novita")
    }
    assert endpoint_privacy_tier(routes["novita"]) < PRIVACY_TIER_ZERO_RETENTION, "fixture"
    assert endpoint_privacy_tier(routes["baseten"]) >= PRIVACY_TIER_ZERO_RETENTION, "fixture"
    for route in routes.values():
        monkeypatch.setitem(MODEL_ENDPOINTS, route.id, route)
    unfiltered = chat_route_endpoint_candidates(
        {"model": "trustedrouter/auto", "messages": []}, Settings()
    )
    assert {endpoint.provider for model, endpoint in unfiltered if model.id == leader} == {
        "baseten", "novita"
    }, "fixture: without a floor, auto takes both routes"

    candidates = chat_route_endpoint_candidates(
        {"model": "trustedrouter/auto", "provider": {"min_privacy": "zdr"}, "messages": []},
        Settings(),
    )
    assert candidates
    leader_routes = [
        endpoint
        for model, endpoint in candidates
        if model.id == leader
    ]
    assert {endpoint.provider for endpoint in leader_routes} == {"baseten"}
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
        for _model, endpoint in candidates
    )


def _manifest_model_ids() -> set[str]:
    # The price refresh tombstones a delisted row; it never deletes one.
    ids: set[str] = set()
    for path in _PROVIDER_MODELS_DIR.glob("*.json"):
        rows = json.loads(path.read_text(encoding="utf-8")).get("models", [])
        ids.update(row["id"] for row in rows if isinstance(row, dict) and row.get("id"))
    return ids


def test_every_auto_candidate_is_a_real_resolvable_model() -> None:
    """A typo'd id is silently dropped by auto_candidate_models, which shrinks
    the ladder without failing anything. A model its hosts delisted leaves the
    ladder the same way, by design, and keeps its manifest row."""
    known = set(MODELS) | _manifest_model_ids()
    unknown = [model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if model_id not in known]
    assert not unknown, f"auto references models no catalog or manifest knows: {unknown}"

    resolved = {model.id for model in auto_candidate_models()}
    dropped = [
        model_id
        for model_id in DEFAULT_AUTO_MODEL_ORDER
        if model_id in MODELS and model_id not in resolved
    ]
    assert not dropped, f"auto candidates silently dropped during resolution: {dropped}"


@pytest.mark.provider_health
def test_every_auto_candidate_is_in_the_catalog() -> None:
    """Live provider state: provider-catalog-health.yml reports it hourly, and
    the price refresh does not wait on it."""
    missing = [model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if model_id not in MODELS]
    assert not missing, f"auto references models absent from the catalog: {missing}"


# --- the guarantee: out-of-bounds requests fail BEFORE a provider is called ---


def test_zero_retention_request_never_yields_a_weaker_endpoint() -> None:
    """`auto` under a ZDR floor must offer only zero-retention endpoints. This
    is what makes keeping Anthropic in the ladder safe."""
    candidates = chat_route_endpoint_candidates(
        {"model": "trustedrouter/auto", "provider": {"min_privacy": "zdr"}, "messages": []},
        Settings(),
    )
    assert candidates, "a ZDR-constrained auto request should still have candidates"
    weak = [
        (model.id, endpoint.provider, endpoint_privacy_tier(endpoint))
        for model, endpoint in candidates
        if endpoint_privacy_tier(endpoint) < PRIVACY_TIER_ZERO_RETENTION
    ]
    assert not weak, f"ZDR request would have been routed to weaker endpoints: {weak}"


def test_explicit_model_below_the_requested_floor_fails_before_dispatch() -> None:
    """Naming a model that cannot meet the request's own privacy bar must be a
    fast 400, not a call to that provider followed by a surprise."""
    with pytest.raises(HTTPException) as raised:
        chat_route_endpoint_candidates(
            {
                "model": "anthropic/claude-sonnet-4.6",
                "provider": {"min_privacy": "zdr"},
                "messages": [],
            },
            Settings(),
        )
    assert raised.value.status_code == 400


def test_unsatisfiable_jurisdiction_fails_before_dispatch() -> None:
    with pytest.raises(HTTPException) as raised:
        chat_route_endpoint_candidates(
            {
                "model": "anthropic/claude-sonnet-4.6",
                "provider": {"jurisdiction": "eu"},
                "messages": [],
            },
            Settings(),
        )
    assert raised.value.status_code == 400
