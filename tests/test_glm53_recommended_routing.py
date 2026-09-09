from __future__ import annotations

from dataclasses import replace

import pytest

from trusted_router.catalog import (
    MODELS,
    endpoint_privacy_tier,
    model_to_openrouter_shape,
)
from trusted_router.catalog_data import PRIVACY_TIER_ZERO_RETENTION
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.routing_candidates import cheap_candidate_models

GLM53_MODELS = ("z-ai/glm-5.3-flash", "z-ai/glm-5.3")


def test_auto_routes_start_with_glm53_recommendations() -> None:
    candidates = chat_route_endpoint_candidates(
        {"model": "trustedrouter/auto", "provider": {"usage": "credits"}}, Settings()
    )
    model_ids = list(dict.fromkeys(model.id for model, _endpoint in candidates))
    assert model_ids[:2] == list(GLM53_MODELS)
    advertised = model_to_openrouter_shape(MODELS["trustedrouter/auto"])
    assert advertised["trustedrouter"]["auto_candidates"][:2] == list(GLM53_MODELS)


def test_cheap_includes_glm53_without_replacing_price_order() -> None:
    candidates = cheap_candidate_models()
    ids = [model.id for model in candidates]
    assert set(GLM53_MODELS) <= set(ids)
    assert len(ids) == len(set(ids)) == 8
    assert len({model.provider for model in candidates}) >= 3
    prices = [
        model.prompt_price_microdollars_per_million_tokens
        + model.completion_price_microdollars_per_million_tokens
        for model in candidates
    ]
    assert prices == sorted(prices)
    assert ids[0] not in GLM53_MODELS
    advertised = model_to_openrouter_shape(MODELS["trustedrouter/cheap"])
    assert advertised["trustedrouter"]["auto_candidates"] == ids


@pytest.mark.parametrize("limit", [0, 1, 2, 4, 8])
def test_cheap_recommendations_respect_pool_limit(limit: int) -> None:
    candidates = cheap_candidate_models(limit=limit)
    assert len(candidates) == limit
    assert len({model.id for model in candidates}) == limit


def test_cheap_small_pool_keeps_the_lowest_price_first() -> None:
    lowest = cheap_candidate_models()[0]
    assert cheap_candidate_models(limit=1) == [lowest]
    assert cheap_candidate_models(limit=2)[0] == lowest


def test_cheap_skips_missing_recommendation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(MODELS, GLM53_MODELS[0])
    ids = [model.id for model in cheap_candidate_models()]
    assert GLM53_MODELS[0] not in ids
    assert GLM53_MODELS[1] in ids
    assert len(ids) == 8


def test_cheap_deduplicates_recommendation_that_is_also_cheapest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = GLM53_MODELS[0]
    monkeypatch.setitem(
        MODELS,
        model_id,
        replace(
            MODELS[model_id],
            prompt_price_microdollars_per_million_tokens=1,
            completion_price_microdollars_per_million_tokens=1,
        ),
    )
    ids = [model.id for model in cheap_candidate_models()]
    assert ids.count(model_id) == 1
    assert GLM53_MODELS[1] in ids
    assert len(ids) == 8


@pytest.mark.parametrize("alias", ["trustedrouter/auto", "trustedrouter/cheap"])
def test_glm53_recommendations_obey_explicit_privacy_floor(alias: str) -> None:
    candidates = chat_route_endpoint_candidates(
        {"model": alias, "provider": {"min_privacy": "zdr", "usage": "credits"}},
        Settings(),
    )
    assert set(GLM53_MODELS) <= {model.id for model, _endpoint in candidates}
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
        for _model, endpoint in candidates
    )
