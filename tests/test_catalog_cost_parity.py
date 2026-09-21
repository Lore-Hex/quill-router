from __future__ import annotations

import pytest

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    Model,
    ModelEndpoint,
    endpoints_for_model,
)
from trusted_router.pricing import _customer_price, select_price_tier
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars


@pytest.mark.parametrize("prompt_tokens", [199_999, 200_000, 200_001])
@pytest.mark.parametrize("cached_tokens", [0, 100_000])
def test_grok_47_billing_matches_xai_at_long_context_boundary(
    prompt_tokens: int, cached_tokens: int,
) -> None:
    model = MODELS["x-ai/grok-4.7"]
    endpoint = MODEL_ENDPOINTS["x-ai/grok-4.7@grok/prepaid"]
    multiplier = 1 if prompt_tokens < 200_000 else 2
    input_rate = _customer_price(2_000_000 * multiplier)
    cached_rate = _customer_price(500_000 * multiplier)
    output_rate = _customer_price(6_000_000 * multiplier)
    tier = select_price_tier(endpoint.price_tiers, prompt_tokens)
    assert tier.prompt_price_microdollars_per_million_tokens == input_rate
    assert tier.prompt_cached_price_microdollars_per_million_tokens == cached_rate
    assert tier.completion_price_microdollars_per_million_tokens == output_rate
    expected = sum(
        (tokens * rate + 500_000) // 1_000_000
        for tokens, rate in (
            (prompt_tokens - cached_tokens, input_rate),
            (cached_tokens, cached_rate),
            (100, output_rate),
        )
    )
    assert cost_microdollars(
        model, prompt_tokens, 100, cached_input_tokens=cached_tokens,
    ) == expected
    assert _endpoint_cost_microdollars(
        # Stage D takes uncached input separately; the model helper takes total input.
        endpoint, prompt_tokens - cached_tokens, 100, cache_read_tokens=cached_tokens,
    ) == expected


def _aligned_credit_endpoints() -> list[tuple[Model, ModelEndpoint]]:
    aligned: list[tuple[Model, ModelEndpoint]] = []
    for model in MODELS.values():
        if not model.supports_chat:
            continue
        for endpoint in endpoints_for_model(model.id):
            if endpoint.usage_type != "Credits":
                continue
            if (
                endpoint.prompt_price_microdollars_per_million_tokens
                != model.prompt_price_microdollars_per_million_tokens
            ):
                continue
            if (
                endpoint.completion_price_microdollars_per_million_tokens
                != model.completion_price_microdollars_per_million_tokens
            ):
                continue
            if endpoint.price_tiers != model.price_tiers:
                continue
            aligned.append((model, endpoint))
    return aligned


def test_aligned_credit_endpoint_costs_match_model_helper_no_cache() -> None:
    aligned = _aligned_credit_endpoints()
    multi_tier_model_ids = {
        model.id for model, _endpoint in aligned if len(model.price_tiers) > 1
    }

    assert aligned
    assert multi_tier_model_ids

    for model, endpoint in aligned:
        for prompt_tokens in (1_000, 100_000, 300_000):
            assert _endpoint_cost_microdollars(
                endpoint,
                prompt_tokens,
                2_000,
            ) == cost_microdollars(
                model,
                prompt_tokens,
                2_000,
            )
