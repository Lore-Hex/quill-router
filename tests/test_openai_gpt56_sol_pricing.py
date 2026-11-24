"""GPT-5.6 Sol leaves its promotion for Standard pricing at 2026-12-01 00:00 UTC."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trusted_router.authorize_candidates import endpoint_cost_microdollars
from trusted_router.catalog import MODEL_ENDPOINTS, effective_endpoint
from trusted_router.money import token_cost_microdollars
from trusted_router.openai_service_tiers import (
    OpenAIPriorityPricing,
    openai_priority_cost_microdollars,
    openai_priority_pricing,
)
from trusted_router.pricing import PriceTier, _flat_tier, select_price_tier
from trusted_router.provider_lifecycle import (
    OPENAI_GPT56_SOL_STANDARD_PRICING_EFFECTIVE_AT,
    ProviderLongContextPrice,
    ProviderPrice,
    openai_gpt56_sol_pricing_period,
    provider_long_context_price,
    provider_price_microdollars,
    provider_pricing_schedule,
)

MODEL_ID = "openai/gpt-5.6-sol"
SWITCH = OPENAI_GPT56_SOL_STANDARD_PRICING_EFFECTIVE_AT
BEFORE = SWITCH - timedelta(seconds=1)
ENDPOINT_IDS = (f"{MODEL_ID}@openai/prepaid", f"{MODEL_ID}@openai/byok")

# Customer rates: upstream x 1.055 (STANDARD_PRICE_MARKUP_BASIS_POINTS).
PROMOTION_TIERS = (
    PriceTier(272_000, 4_220_000, 21_100_000, 422_000),
    PriceTier(None, 8_440_000, 31_650_000, 844_000),
)
STANDARD_TIERS = (
    PriceTier(272_000, 5_275_000, 31_650_000, 527_500),
    PriceTier(None, 10_550_000, 47_475_000, 1_055_000),
)


def test_switch_is_december_first_utc_midnight() -> None:
    assert SWITCH == datetime(2026, 12, 1, 0, 0, tzinfo=UTC)


def test_upstream_short_context_price_changes_exactly_at_switch() -> None:
    assert provider_price_microdollars("openai", MODEL_ID, at=BEFORE) == ProviderPrice(
        4_000_000, 20_000_000, 400_000
    )
    assert provider_price_microdollars("openai", MODEL_ID, at=SWITCH) == ProviderPrice(
        5_000_000, 30_000_000, 500_000
    )


def test_upstream_long_context_price_changes_exactly_at_switch() -> None:
    assert provider_long_context_price("openai", MODEL_ID, at=BEFORE) == (
        ProviderLongContextPrice(272_000, ProviderPrice(8_000_000, 30_000_000, 800_000))
    )
    assert provider_long_context_price("openai", MODEL_ID, at=SWITCH) == (
        ProviderLongContextPrice(272_000, ProviderPrice(10_000_000, 45_000_000, 1_000_000))
    )


@pytest.mark.parametrize("endpoint_id", ENDPOINT_IDS)
def test_before_switch_the_override_matches_todays_catalog_price(endpoint_id: str) -> None:
    endpoint = MODEL_ENDPOINTS[endpoint_id]
    before = effective_endpoint(endpoint, at=BEFORE)

    assert before.price_tiers == PROMOTION_TIERS
    assert before.published_price_tiers == PROMOTION_TIERS
    assert before.prompt_price_microdollars_per_million_tokens == 4_220_000
    assert before.completion_price_microdollars_per_million_tokens == 21_100_000


@pytest.mark.parametrize("endpoint_id", ENDPOINT_IDS)
def test_from_switch_customers_pay_standard_rates(endpoint_id: str) -> None:
    after = effective_endpoint(MODEL_ENDPOINTS[endpoint_id], at=SWITCH)

    assert after.price_tiers == STANDARD_TIERS
    assert after.published_price_tiers == STANDARD_TIERS
    assert after.prompt_price_microdollars_per_million_tokens == 5_275_000
    assert after.completion_price_microdollars_per_million_tokens == 31_650_000
    assert after.published_prompt_price_microdollars_per_million_tokens == 5_275_000
    assert after.published_completion_price_microdollars_per_million_tokens == 31_650_000


@pytest.mark.parametrize(
    ("at", "prompt_tokens", "expected"),
    [
        (BEFORE, 272_000, (4_220_000, 422_000, 21_100_000)),
        (BEFORE, 272_001, (8_440_000, 844_000, 31_650_000)),
        (SWITCH, 272_000, (5_275_000, 527_500, 31_650_000)),
        (SWITCH, 272_001, (10_550_000, 1_055_000, 47_475_000)),
    ],
)
def test_input_cached_input_and_output_rates_by_context_size(
    at: datetime, prompt_tokens: int, expected: tuple[int, int, int]
) -> None:
    endpoint = effective_endpoint(MODEL_ENDPOINTS[ENDPOINT_IDS[0]], at=at)
    tier = select_price_tier(endpoint.price_tiers, prompt_tokens)

    assert (
        tier.prompt_price_microdollars_per_million_tokens,
        tier.prompt_cached_price_microdollars_per_million_tokens,
        tier.completion_price_microdollars_per_million_tokens,
    ) == expected


@pytest.mark.parametrize(
    ("at", "manifest_tiers", "expected"),
    [
        # OpenAI's page publishes Standard early: still bill the promotion.
        (BEFORE, STANDARD_TIERS, PROMOTION_TIERS),
        # The hourly refresh has not yet seen Standard: still bill Standard.
        (SWITCH, PROMOTION_TIERS, STANDARD_TIERS),
        # A refresh that collapsed the tiers cannot drop the long-context rate.
        (SWITCH, _flat_tier(4_220_000, 21_100_000, 422_000), STANDARD_TIERS),
    ],
)
def test_refreshed_manifest_prices_cannot_move_the_switch(
    at: datetime, manifest_tiers: tuple[PriceTier, ...], expected: tuple[PriceTier, ...]
) -> None:
    refreshed = replace(
        MODEL_ENDPOINTS[ENDPOINT_IDS[0]],
        prompt_price_microdollars_per_million_tokens=manifest_tiers[0].prompt_price_microdollars_per_million_tokens,
        completion_price_microdollars_per_million_tokens=manifest_tiers[0].completion_price_microdollars_per_million_tokens,
        price_tiers=manifest_tiers,
        published_price_tiers=manifest_tiers,
    )

    assert effective_endpoint(refreshed, at=at).price_tiers == expected


def test_schedule_is_public_and_authorization_locked() -> None:
    expected = {
        "kind": "fixed_cutover",
        "timezone": "UTC",
        "effective_at": "2026-12-01T00:00:00Z",
        "rate_locked_at": "authorization",
    }
    assert provider_pricing_schedule("openai", MODEL_ID, at=BEFORE) == {
        **expected,
        "current_period": "promotion",
    }
    assert provider_pricing_schedule("openai", MODEL_ID, at=SWITCH) == {
        **expected,
        "current_period": "standard",
    }


def test_only_first_party_openai_sol_is_pinned() -> None:
    # Third-party resellers publish their own prices; leave them to refresh.
    for provider in ("atlas-cloud", "gmi", "boundless", "azure"):
        assert provider_price_microdollars(provider, MODEL_ID, at=SWITCH) is None
        assert provider_long_context_price(provider, MODEL_ID, at=SWITCH) is None
    for model_id in ("openai/gpt-5.6-terra", "openai/gpt-5.6-luna"):
        assert provider_price_microdollars("openai", model_id, at=SWITCH) is None


# Fast (Priority) customer rates: upstream x 1.055; cache writes 1.25x input.
# Promotion upstream $8 / $0.80 / $40; Standard upstream $10 / $1 / $60.
FAST_PROMOTION = OpenAIPriorityPricing(8_440_000, 844_000, 42_200_000, 10_550_000)
FAST_STANDARD = OpenAIPriorityPricing(10_550_000, 1_055_000, 63_300_000, 13_187_500)


def test_fast_and_standard_tiers_share_one_period() -> None:
    assert openai_gpt56_sol_pricing_period(BEFORE) == "promotion"
    assert openai_gpt56_sol_pricing_period(SWITCH) == "standard"
    assert openai_gpt56_sol_pricing_period("2026-11-30T23:59:59Z") == "promotion"
    assert openai_gpt56_sol_pricing_period("2026-12-01T00:00:00Z") == "standard"


@pytest.mark.parametrize(
    ("at", "expected"), [(BEFORE, FAST_PROMOTION), (SWITCH, FAST_STANDARD)]
)
def test_fast_customer_rates_change_exactly_at_switch(
    at: datetime, expected: OpenAIPriorityPricing
) -> None:
    assert openai_priority_pricing(MODEL_ID, at=at) == expected


@pytest.mark.parametrize(
    ("at", "token_class", "rate"),
    [
        (BEFORE, "input", 8_440_000),
        (BEFORE, "cached_input", 844_000),
        (BEFORE, "cache_write", 10_550_000),
        (BEFORE, "output", 42_200_000),
        (SWITCH, "input", 10_550_000),
        (SWITCH, "cached_input", 1_055_000),
        (SWITCH, "cache_write", 13_187_500),
        (SWITCH, "output", 63_300_000),
    ],
)
@pytest.mark.parametrize("endpoint_id", ENDPOINT_IDS)
@pytest.mark.parametrize("service_tier", ["priority", "auto"])
def test_fast_cost_per_token_class_is_locked_to_the_authorization_time(
    endpoint_id: str, service_tier: str, at: datetime, token_class: str, rate: int
) -> None:
    tokens = 1_000_000
    counts = {
        "input": (tokens, 0, 0, 0),
        "cached_input": (0, 0, tokens, 0),
        "cache_write": (0, 0, 0, tokens),
        "output": (0, tokens, 0, 0),
    }[token_class]
    input_tokens, output_tokens, cache_read, cache_creation = counts

    # Settlement prices at authorization.created_at, so a request authorized
    # one second before the switch keeps the promotion whenever it settles.
    assert endpoint_cost_microdollars(
        MODEL_ENDPOINTS[endpoint_id],
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        effective_at=at,
        service_tier=service_tier,
        reserve_auto=True,
    ) == token_cost_microdollars(tokens, rate)
    assert openai_priority_cost_microdollars(
        MODEL_ID,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        effective_at=at,
    ) == token_cost_microdollars(tokens, rate)


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        (
            "openai/gpt-5.6-terra",
            OpenAIPriorityPricing(5_275_000, 527_500, 31_650_000, 6_593_750),
        ),
        (
            "openai/gpt-5.6-luna",
            OpenAIPriorityPricing(2_110_000, 211_000, 12_660_000, 2_637_500),
        ),
    ],
)
def test_other_models_fast_prices_do_not_move_at_the_switch(
    model_id: str, expected: OpenAIPriorityPricing
) -> None:
    assert openai_priority_pricing(model_id, at=BEFORE) == expected
    assert openai_priority_pricing(model_id, at=SWITCH) == expected


def test_models_without_fast_pricing_stay_unpriced() -> None:
    for model_id in ("openai/gpt-4o-mini", "anthropic/claude-haiku-4.5"):
        assert openai_priority_pricing(model_id, at=BEFORE) is None
        assert openai_priority_pricing(model_id, at=SWITCH) is None
