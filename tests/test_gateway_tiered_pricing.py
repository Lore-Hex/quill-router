from __future__ import annotations

from trusted_router.catalog import (
    MODELS,
    ModelEndpoint,
    cache_token_prices_microdollars,
    endpoint_for_id,
    endpoints_for_model,
)
from trusted_router.money import token_cost_microdollars
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars


def _gemini_pro_credits_endpoint() -> ModelEndpoint:
    endpoint = next(
        endpoint
        for endpoint in endpoints_for_model("google/gemini-2.5-pro")
        if endpoint.provider == "gemini" and endpoint.usage_type == "Credits"
    )
    assert len(endpoint.price_tiers) >= 2
    return endpoint


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
        read_price, write_price = cache_token_prices_microdollars(endpoint.provider, prompt_price)
        cost += token_cost_microdollars(cache_read_tokens, read_price)
        cost += token_cost_microdollars(cache_creation_tokens, write_price)
    return cost


def test_endpoint_cost_uses_high_tier_for_large_prompt() -> None:
    endpoint = _gemini_pro_credits_endpoint()

    expected = _tier_cost(endpoint, 300_000, 2_000, tier_index=1)

    assert _endpoint_cost_microdollars(endpoint, 300_000, 2_000) == expected
    assert expected > _headline_cost(endpoint, 300_000, 2_000)


def test_endpoint_cost_keeps_headline_cost_below_threshold() -> None:
    endpoint = _gemini_pro_credits_endpoint()

    assert _endpoint_cost_microdollars(endpoint, 100_000, 2_000) == _headline_cost(
        endpoint, 100_000, 2_000
    )


def test_endpoint_cost_tier_threshold_is_inclusive() -> None:
    endpoint = _gemini_pro_credits_endpoint()
    threshold = endpoint.price_tiers[0].max_prompt_tokens
    assert threshold is not None

    assert _endpoint_cost_microdollars(endpoint, threshold, 2_000) == _tier_cost(
        endpoint, threshold, 2_000, tier_index=0
    )
    assert _endpoint_cost_microdollars(endpoint, threshold + 1, 2_000) == _tier_cost(
        endpoint, threshold + 1, 2_000, tier_index=1
    )


def test_endpoint_cost_uses_total_prompt_for_cached_tier_selection() -> None:
    endpoint = _gemini_pro_credits_endpoint()

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
    endpoint = _gemini_pro_credits_endpoint()
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
        assert _endpoint_cost_microdollars(
            endpoint, prompt_tokens, 2_000
        ) == cost_microdollars(model, prompt_tokens, 2_000)
