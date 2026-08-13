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

import pytest
from fastapi import HTTPException

from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, endpoint_privacy_tier
from trusted_router.catalog_data import (
    DEFAULT_AUTO_MODEL_ORDER,
    PRIVACY_TIER_ZERO_RETENTION,
    US_FOCUSED_PROVIDER_ORDER,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.routing_candidates import auto_candidate_models

# The models the default route should reach for after the global 0813 leader.
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


def test_auto_ladder_spans_more_than_one_provider() -> None:
    """A single-provider ladder makes one provider outage an `auto` outage."""
    providers: set[str] = set()
    qualifying = [
        model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if _us_zdr_providers(model_id)
    ][:QUALIFYING_LEAD_MODELS]
    for model_id in qualifying:
        providers |= _us_zdr_providers(model_id)
    assert len(providers) > 1, f"the leading auto candidates share one provider: {providers}"


def test_current_release_then_cheap_qualifying_models_lead_the_ladder() -> None:
    assert DEFAULT_AUTO_MODEL_ORDER[:4] == [
        "deepseek/deepseek-v4-pro-0813",
        "deepseek/deepseek-v4-flash-0731",
        "moonshotai/kimi-k3",
        "z-ai/glm-5.2",
    ]


def test_zdr_filter_skips_incompatible_global_leader_before_dispatch() -> None:
    candidates = chat_route_endpoint_candidates(
        {"model": "trustedrouter/auto", "provider": {"min_privacy": "zdr"}, "messages": []},
        Settings(),
    )
    assert candidates
    assert all(model.id != "deepseek/deepseek-v4-pro-0813" for model, _endpoint in candidates)


def test_every_auto_candidate_is_a_real_resolvable_model() -> None:
    """A typo'd id is silently dropped by auto_candidate_models, which shrinks
    the ladder without failing anything."""
    missing = [model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if model_id not in MODELS]
    assert not missing, f"auto references models absent from the catalog: {missing}"

    resolved = {model.id for model in auto_candidate_models()}
    dropped = [model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if model_id not in resolved]
    assert not dropped, f"auto candidates silently dropped during resolution: {dropped}"


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
