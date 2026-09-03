from __future__ import annotations

import random
from datetime import UTC, datetime

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    ModelEndpoint,
    cache_token_prices_microdollars,
    effective_endpoint,
)
from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import resolve_request_rates
from trusted_router.routes.internal.gateway import (
    _endpoint_cost_microdollars,
    _endpoint_cost_microdollars_from_document,
)
from trusted_router.stage_d import endpoint_pricing_document

EFFECTIVE_AT = datetime(2026, 9, 2, 12, tzinfo=UTC)
ROUNDING_RESIDUE_COUNTS = (
    0,
    1,
    2,
    3,
    7,
    499_999,
    500_000,
    500_001,
    999_999,
    1_000_000,
    1_000_001,
)


def _assert_equivalent(
    endpoint: ModelEndpoint,
    document: dict[str, object],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    price_tier_input_tokens: int | None = None,
) -> None:
    endpoint_id = endpoint.id
    expected = _legacy_catalog_cost(
        endpoint,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        price_tier_input_tokens=price_tier_input_tokens,
    )
    live = _endpoint_cost_microdollars(
        endpoint,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        price_tier_input_tokens=price_tier_input_tokens,
        effective_at=EFFECTIVE_AT,
    )
    actual = _endpoint_cost_microdollars_from_document(
        document,
        endpoint_id,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        price_tier_input_tokens=price_tier_input_tokens,
    )
    assert actual == live == expected, (endpoint_id, input_tokens, output_tokens)


def _legacy_catalog_cost(
    endpoint: ModelEndpoint,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    price_tier_input_tokens: int | None,
) -> int:
    """Independent copy of the pre-refactor catalog pricing contract."""
    total_prompt = input_tokens + cache_read_tokens + cache_creation_tokens
    tier_prompt = total_prompt
    if (
        price_tier_input_tokens is not None
        and price_tier_input_tokens > 0
        and price_tier_input_tokens <= total_prompt
    ):
        tier_prompt = price_tier_input_tokens
    rates = resolve_request_rates(
        endpoint.price_tiers,
        headline_prompt_micro_per_m=(
            endpoint.prompt_price_microdollars_per_million_tokens
        ),
        headline_completion_micro_per_m=(
            endpoint.completion_price_microdollars_per_million_tokens
        ),
        total_prompt_tokens=tier_prompt,
    )
    input_rate = rates.prompt_price_microdollars_per_million_tokens
    output_rate = rates.completion_price_microdollars_per_million_tokens
    default_cached, creation_rate = cache_token_prices_microdollars(
        endpoint.provider, input_rate
    )
    cached_rate = (
        rates.prompt_cached_price_microdollars_per_million_tokens
        if rates.prompt_cached_price_microdollars_per_million_tokens is not None
        else default_cached
    )
    cost = (
        endpoint.request_price_microdollars
        + token_cost_microdollars(input_tokens, input_rate)
        + token_cost_microdollars(output_tokens, output_rate)
        + token_cost_microdollars(cache_read_tokens, cached_rate)
        + token_cost_microdollars(cache_creation_tokens, creation_rate)
    )
    positive = (
        endpoint.request_price_microdollars > 0
        or (input_tokens > 0 and input_rate > 0)
        or (output_tokens > 0 and output_rate > 0)
        or (cache_read_tokens > 0 and cached_rate > 0)
        or (cache_creation_tokens > 0 and creation_rate > 0)
    )
    return max(cost, 1) if positive else 0


def test_pricing_document_is_equivalent_to_every_catalog_endpoint() -> None:
    for raw_endpoint in MODEL_ENDPOINTS.values():
        endpoint = effective_endpoint(raw_endpoint, at=EFFECTIVE_AT)
        document = endpoint_pricing_document((endpoint,))

        # Zero and tiny positive usage exercise the one-micro floor.
        _assert_equivalent(endpoint, document, input_tokens=0, output_tokens=0)
        _assert_equivalent(endpoint, document, input_tokens=1, output_tokens=0)
        _assert_equivalent(endpoint, document, input_tokens=0, output_tokens=1)

        for count in ROUNDING_RESIDUE_COUNTS:
            _assert_equivalent(
                endpoint,
                document,
                input_tokens=count,
                output_tokens=count,
                cache_read_tokens=count // 3,
                cache_creation_tokens=count // 7,
            )

        for tier in endpoint.price_tiers:
            if tier.max_prompt_tokens is None:
                continue
            for offset in (-1, 0, 1):
                prompt = max(0, tier.max_prompt_tokens + offset)
                _assert_equivalent(
                    endpoint,
                    document,
                    input_tokens=prompt,
                    output_tokens=17,
                )

        rng = random.Random(f"stage-d:{endpoint.id}")  # noqa: S311 - deterministic test cases
        for _ in range(1_000):
            input_tokens = rng.randrange(0, 2_000_001)
            output_tokens = rng.randrange(0, 200_001)
            cache_read = rng.randrange(0, input_tokens + 1)
            cache_creation = rng.randrange(0, 10_001)
            total_prompt = input_tokens + cache_read + cache_creation
            tier_basis = rng.randrange(0, total_prompt + 1) if total_prompt else None
            _assert_equivalent(
                endpoint,
                document,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                price_tier_input_tokens=tier_basis,
            )
