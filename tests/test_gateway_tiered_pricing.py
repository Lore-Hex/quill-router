from __future__ import annotations

from dataclasses import replace

import pytest

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    ModelEndpoint,
    cache_token_prices_microdollars,
    endpoint_for_id,
)
from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import _read_pricing_tiers
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.routes.internal.gateway import (
    _endpoint_cost_microdollars,
    _provider_price_tier_input_tokens,
)


def _tiered_credits_endpoint() -> ModelEndpoint:
    """Any google-ai-studio Credits endpoint with tiered pricing.

    Deliberately NOT pinned to a model id. These tests exercise tiered-pricing
    arithmetic, and the expected values are derived from whichever endpoint is
    returned, so the specific model is irrelevant to what is being tested.
    Hardcoding one turns an ordinary vendor retirement into an unrelated test
    failure — which is exactly what happened when Google retired
    gemini-2.5-pro on AI Studio and it left the catalog.

    Sorted for determinism so a catalog addition cannot silently change which
    endpoint the suite runs against.
    """
    candidates = sorted(
        (
            endpoint
            for endpoint in MODEL_ENDPOINTS.values()
            if endpoint.provider == "google-ai-studio"
            and endpoint.usage_type == "Credits"
            and len(getattr(endpoint, "price_tiers", ()) or ()) >= 2
        ),
        key=lambda endpoint: endpoint.model_id,
    )
    assert candidates, "catalog has no multi-tier google-ai-studio Credits endpoint"
    return candidates[0]


def _sakana_fugu_pricing_fixture() -> ModelEndpoint:
    """Return the live direct Fugu route with its pass-through retail tiers."""

    endpoint = endpoint_for_id("sakana-ai/fugu-ultra-v1.1@sakana/prepaid")
    assert endpoint is not None
    return endpoint


def test_provider_tier_basis_is_scoped_to_sakana_fugu() -> None:
    fugu = _sakana_fugu_pricing_fixture()
    assert _provider_price_tier_input_tokens(fugu, 6) == 6
    assert _provider_price_tier_input_tokens(
        replace(fugu, model_id="sakana-ai/sakana-namazu-v1.0"),
        6,
    ) is None
    assert _provider_price_tier_input_tokens(
        replace(fugu, provider="example"),
        6,
    ) is None


@pytest.mark.parametrize(
    "prompt_tiers,completion_tiers",
    [
        (
            [{"max_prompt_tokens": 272_000, "prompt": "0.000005"}],
            [{"max_prompt_tokens": 272_000, "completion": "0.00003"}],
        ),
        (
            [
                {"max_prompt_tokens": 500_000, "prompt": "0.000005"},
                {"max_prompt_tokens": 272_000, "prompt": "0.00001"},
                {"max_prompt_tokens": None, "prompt": "0.00001"},
            ],
            [
                {"max_prompt_tokens": 500_000, "completion": "0.00003"},
                {"max_prompt_tokens": 272_000, "completion": "0.000045"},
                {"max_prompt_tokens": None, "completion": "0.000045"},
            ],
        ),
        (
            [
                {"max_prompt_tokens": None, "prompt": "0.000005"},
                {"max_prompt_tokens": None, "prompt": "0.00001"},
            ],
            [
                {"max_prompt_tokens": None, "completion": "0.00003"},
                {"max_prompt_tokens": None, "completion": "0.000045"},
            ],
        ),
        (
            [
                {"max_prompt_tokens": 272_000, "prompt": "broken"},
                {"max_prompt_tokens": None, "prompt": "0.00001"},
            ],
            [
                {"max_prompt_tokens": 272_000, "completion": "0.00003"},
                {"max_prompt_tokens": None, "completion": "0.000045"},
            ],
        ),
        (
            [
                {"max_prompt_tokens": 272_000, "prompt": "0.00001"},
                {"max_prompt_tokens": None, "prompt": "0.000005"},
            ],
            [
                {"max_prompt_tokens": 272_000, "completion": "0.000045"},
                {"max_prompt_tokens": None, "completion": "0.00003"},
            ],
        ),
    ],
)
def test_snapshot_price_tiers_fail_closed(
    prompt_tiers: list[dict[str, object]],
    completion_tiers: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError):
        _read_pricing_tiers(
            {
                "prompt_tiers": prompt_tiers,
                "completion_tiers": completion_tiers,
            },
            "prompt",
        )


def test_snapshot_price_tiers_fail_closed_when_only_one_side_is_present() -> None:
    with pytest.raises(ValueError):
        _read_pricing_tiers(
            {
                "prompt_tiers": [
                    {
                        "max_prompt_tokens": None,
                        "prompt": "0.000005",
                    }
                ]
            },
            "prompt",
        )


def _headline_cost(
    endpoint: ModelEndpoint,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    cost = token_cost_microdollars(
        input_tokens, endpoint.prompt_price_microdollars_per_million_tokens
    ) + token_cost_microdollars(
        output_tokens, endpoint.completion_price_microdollars_per_million_tokens
    )
    if cache_read_tokens or cache_creation_tokens:
        read_price, write_price = cache_token_prices_microdollars(
            endpoint.provider, endpoint.prompt_price_microdollars_per_million_tokens
        )
        cost += token_cost_microdollars(cache_read_tokens, read_price)
        cost += token_cost_microdollars(cache_creation_tokens, write_price)
    return cost


def _tier_cost(
    endpoint: ModelEndpoint,
    input_tokens: int,
    output_tokens: int,
    *,
    tier_index: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    tier = endpoint.price_tiers[tier_index]
    prompt_price = tier.prompt_price_microdollars_per_million_tokens
    cost = token_cost_microdollars(input_tokens, prompt_price) + token_cost_microdollars(
        output_tokens, tier.completion_price_microdollars_per_million_tokens
    )
    if cache_read_tokens or cache_creation_tokens:
        default_read_price, write_price = cache_token_prices_microdollars(
            endpoint.provider, prompt_price
        )
        read_price = (
            tier.prompt_cached_price_microdollars_per_million_tokens
            if tier.prompt_cached_price_microdollars_per_million_tokens is not None
            else default_read_price
        )
        cost += token_cost_microdollars(cache_read_tokens, read_price)
        cost += token_cost_microdollars(cache_creation_tokens, write_price)
    return cost


def test_endpoint_cost_uses_high_tier_for_large_prompt() -> None:
    endpoint = _tiered_credits_endpoint()

    expected = _tier_cost(endpoint, 300_000, 2_000, tier_index=1)

    assert _endpoint_cost_microdollars(endpoint, 300_000, 2_000) == expected
    assert expected > _headline_cost(endpoint, 300_000, 2_000)


def test_endpoint_cost_keeps_headline_cost_below_threshold() -> None:
    endpoint = _tiered_credits_endpoint()

    assert _endpoint_cost_microdollars(endpoint, 100_000, 2_000) == _headline_cost(
        endpoint, 100_000, 2_000
    )


def test_endpoint_cost_tier_threshold_is_inclusive() -> None:
    endpoint = _tiered_credits_endpoint()
    threshold = endpoint.price_tiers[0].max_prompt_tokens
    assert threshold is not None

    assert _endpoint_cost_microdollars(endpoint, threshold, 2_000) == _tier_cost(
        endpoint, threshold, 2_000, tier_index=0
    )
    assert _endpoint_cost_microdollars(endpoint, threshold + 1, 2_000) == _tier_cost(
        endpoint, threshold + 1, 2_000, tier_index=1
    )


def test_endpoint_cost_uses_total_prompt_for_cached_tier_selection() -> None:
    endpoint = _tiered_credits_endpoint()

    expected = _tier_cost(
        endpoint,
        150_000,
        2_000,
        tier_index=1,
        cache_read_tokens=150_000,
    )

    assert (
        _endpoint_cost_microdollars(
            endpoint,
            150_000,
            2_000,
            cache_read_tokens=150_000,
        )
        == expected
    )
    assert expected > _headline_cost(
        endpoint,
        150_000,
        2_000,
        cache_read_tokens=150_000,
    )


def test_endpoint_cost_can_use_provider_metered_context_for_tier_only() -> None:
    endpoint = _sakana_fugu_pricing_fixture()

    # Sakana bills all 300K input tokens but selects Fugu's context tier from
    # the 100K initial request context. actual_input_tokens includes the 200K
    # provider-side orchestration tokens; they are not cache reads.
    expected = _tier_cost(endpoint, 300_000, 2_000, tier_index=0)
    assert (
        _endpoint_cost_microdollars(
            endpoint,
            300_000,
            2_000,
            price_tier_input_tokens=100_000,
        )
        == expected
    )


def test_endpoint_cost_rejects_invalid_tier_basis_conservatively() -> None:
    endpoint = _sakana_fugu_pricing_fixture()
    expected = _tier_cost(endpoint, 300_000, 2_000, tier_index=1)
    assert (
        _endpoint_cost_microdollars(
            endpoint,
            300_000,
            2_000,
            price_tier_input_tokens=300_001,
        )
        == expected
    )


def test_endpoint_cost_flat_and_empty_tiers_match_headline_math_with_cache() -> None:
    single_tier = endpoint_for_id("anthropic/claude-haiku-4.5@anthropic/prepaid")
    assert single_tier is not None
    assert len(single_tier.price_tiers) == 1
    assert _endpoint_cost_microdollars(
        single_tier,
        1_234,
        567,
        cache_read_tokens=890,
        cache_creation_tokens=321,
    ) == _headline_cost(
        single_tier,
        1_234,
        567,
        cache_read_tokens=890,
        cache_creation_tokens=321,
    )

    empty_tiers = endpoint_for_id("openai/text-embedding-3-large@openai/prepaid")
    assert empty_tiers is not None
    assert empty_tiers.price_tiers == ()
    assert _endpoint_cost_microdollars(
        empty_tiers,
        1_234,
        0,
        cache_read_tokens=890,
        cache_creation_tokens=321,
    ) == _headline_cost(
        empty_tiers,
        1_234,
        0,
        cache_read_tokens=890,
        cache_creation_tokens=321,
    )


def test_endpoint_cost_matches_model_helper_for_multitier_no_cache() -> None:
    endpoint = _tiered_credits_endpoint()
    model = MODELS[endpoint.model_id]
    assert endpoint.price_tiers == model.price_tiers
    assert (
        endpoint.prompt_price_microdollars_per_million_tokens
        == model.prompt_price_microdollars_per_million_tokens
    )
    assert (
        endpoint.completion_price_microdollars_per_million_tokens
        == model.completion_price_microdollars_per_million_tokens
    )

    for prompt_tokens in (100_000, 300_000):
        assert _endpoint_cost_microdollars(endpoint, prompt_tokens, 2_000) == cost_microdollars(
            model, prompt_tokens, 2_000
        )


def test_endpoint_cost_reserves_one_microdollar_for_positive_fractional_cost() -> None:
    endpoint = ModelEndpoint(
        id="test/tiny@test/prepaid",
        model_id="test/tiny",
        provider="openai",
        usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=1,
        completion_price_microdollars_per_million_tokens=1,
    )

    assert _endpoint_cost_microdollars(endpoint, 1, 1) == 1
    assert _endpoint_cost_microdollars(endpoint, 0, 0) == 0
