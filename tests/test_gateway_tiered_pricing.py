from __future__ import annotations

import json
from dataclasses import replace

import pytest

from trusted_router.catalog import (
    ModelEndpoint,
    cache_token_prices_microdollars,
    endpoint_for_id,
)
from trusted_router.catalog_data import Model, PriceTier
from trusted_router.catalog_ingest import _PROVIDER_MODELS_DIR
from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import _read_pricing_tiers, customer_fixed_price_microdollars
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.routes.internal.gateway import (
    _endpoint_cost_microdollars,
    _provider_price_tier_input_tokens,
)


def _tiered_credits_endpoint() -> ModelEndpoint:
    """A google-ai-studio Credits route with Gemini-Pro-shape tiered pricing.

    A fixture: the tiered-pricing arithmetic holds for any tiered route, and
    which tiered models AI Studio lists today is provider state.
    """
    tiers = (
        PriceTier(
            max_prompt_tokens=200_000,
            prompt_price_microdollars_per_million_tokens=2_110_000,
            completion_price_microdollars_per_million_tokens=12_660_000,
            prompt_cached_price_microdollars_per_million_tokens=211_000,
        ),
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=4_220_000,
            completion_price_microdollars_per_million_tokens=18_990_000,
            prompt_cached_price_microdollars_per_million_tokens=422_000,
        ),
    )
    return ModelEndpoint(
        id="google/tiered-fixture@google-ai-studio/prepaid",
        model_id="google/tiered-fixture",
        provider="google-ai-studio",
        usage_type="Credits",
        upstream_id="tiered-fixture",
        prompt_price_microdollars_per_million_tokens=2_110_000,
        completion_price_microdollars_per_million_tokens=12_660_000,
        published_prompt_price_microdollars_per_million_tokens=2_110_000,
        published_completion_price_microdollars_per_million_tokens=12_660_000,
        price_tiers=tiers,
        published_price_tiers=tiers,
    )


def _sakana_fugu_pricing_fixture() -> ModelEndpoint:
    """The direct Fugu route with its pass-through retail tiers, as a fixture:
    whether Sakana lists Fugu today is provider state."""

    tiers = (
        PriceTier(
            max_prompt_tokens=272_000,
            prompt_price_microdollars_per_million_tokens=5_000_000,
            completion_price_microdollars_per_million_tokens=30_000_000,
            prompt_cached_price_microdollars_per_million_tokens=500_000,
        ),
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=10_000_000,
            completion_price_microdollars_per_million_tokens=45_000_000,
            prompt_cached_price_microdollars_per_million_tokens=1_000_000,
        ),
    )
    return ModelEndpoint(
        id="sakana-ai/fugu-ultra-v1.1@sakana/prepaid",
        model_id="sakana-ai/fugu-ultra-v1.1",
        provider="sakana",
        usage_type="Credits",
        upstream_id="fugu-ultra-v1.1",
        prompt_price_microdollars_per_million_tokens=5_000_000,
        completion_price_microdollars_per_million_tokens=30_000_000,
        published_prompt_price_microdollars_per_million_tokens=5_000_000,
        published_completion_price_microdollars_per_million_tokens=30_000_000,
        price_tiers=tiers,
        published_price_tiers=tiers,
    )


def test_provider_tier_basis_is_scoped_to_sakana_fugu() -> None:
    fugu = _sakana_fugu_pricing_fixture()
    assert _provider_price_tier_input_tokens(fugu, 6) == 6
    assert (
        _provider_price_tier_input_tokens(
            replace(fugu, model_id="sakana-ai/sakana-namazu-v1.0"),
            6,
        )
        is None
    )
    assert (
        _provider_price_tier_input_tokens(
            replace(fugu, provider="example"),
            6,
        )
        is None
    )


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
    # Fixtures: the arithmetic holds for any route with one uncapped tier or
    # none, and what a host charges today is provider state.
    flat = PriceTier(
        max_prompt_tokens=None,
        prompt_price_microdollars_per_million_tokens=1_000_000,
        completion_price_microdollars_per_million_tokens=5_000_000,
    )
    single_tier = ModelEndpoint(
        id="anthropic/flat-fixture@anthropic/prepaid",
        model_id="anthropic/flat-fixture",
        provider="anthropic",
        usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=1_000_000,
        completion_price_microdollars_per_million_tokens=5_000_000,
        price_tiers=(flat,),
        published_price_tiers=(flat,),
    )
    # 1,234 input tokens at $1/M, 567 output at $5/M, 890 cache reads at
    # Anthropic's 0.1x input and 321 cache writes at its 1.25x input.
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
    ) == 1_234 + 2_835 + 89 + 401

    empty_tiers = ModelEndpoint(
        id="openai/untiered-fixture@openai/prepaid",
        model_id="openai/untiered-fixture",
        provider="openai",
        usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=100_000,
        completion_price_microdollars_per_million_tokens=0,
    )
    # 1,234 input tokens at $0.10/M, 890 cache reads at OpenAI's 0.5x input
    # and 321 cache writes at its 1.25x input.
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
    ) == 123 + 45 + 40


def test_endpoint_cost_matches_model_helper_for_multitier_no_cache() -> None:
    endpoint = _tiered_credits_endpoint()
    model = Model(
        id=endpoint.model_id,
        name=endpoint.model_id,
        provider=endpoint.provider,
        context_length=1_048_576,
        prompt_price_microdollars_per_million_tokens=(
            endpoint.prompt_price_microdollars_per_million_tokens
        ),
        completion_price_microdollars_per_million_tokens=(
            endpoint.completion_price_microdollars_per_million_tokens
        ),
        price_tiers=endpoint.price_tiers,
        published_price_tiers=endpoint.published_price_tiers,
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


def test_perplexity_sonar_charges_fixed_request_fee_and_tokens_once() -> None:
    # A fixture priced like Sonar; the published route is checked below.
    prices = {
        "prompt_price_microdollars_per_million_tokens": 263_750,
        "completion_price_microdollars_per_million_tokens": 2_637_500,
        "request_price_microdollars": 5_275,
    }
    endpoint = ModelEndpoint(
        id="perplexity/sonar@perplexity/prepaid",
        model_id="perplexity/sonar",
        provider="perplexity",
        usage_type="Credits",
        upstream_id="sonar",
        **prices,
    )
    model = Model(
        id="perplexity/sonar", name="Sonar", provider="perplexity", context_length=127_072,
        **prices,
    )
    expected = (
        5_275
        + token_cost_microdollars(1_000, endpoint.prompt_price_microdollars_per_million_tokens)
        + token_cost_microdollars(100, endpoint.completion_price_microdollars_per_million_tokens)
    )
    assert _endpoint_cost_microdollars(endpoint, 1_000, 100) == expected
    assert cost_microdollars(model, 1_000, 100) == expected
    assert _endpoint_cost_microdollars(endpoint, 0, 0) == 5_275


def test_perplexity_routes_publish_the_manifest_request_fee() -> None:
    # Every routable row of the committed manifest; a delisted one is not expected.
    raw = json.loads((_PROVIDER_MODELS_DIR / "perplexity.json").read_text(encoding="utf-8"))
    for row in raw["models"]:
        if row.get("routable") is False:
            continue
        endpoint = endpoint_for_id(f"{row['id']}@perplexity/prepaid")
        assert endpoint is not None, row["id"]
        assert endpoint.upstream_id == row["upstream_id"]
        assert endpoint.request_price_microdollars == customer_fixed_price_microdollars(
            row["fixed_request_price_microdollars"]
        )
