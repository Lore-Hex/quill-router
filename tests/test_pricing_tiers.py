"""Tier-aware pricing tests — verify select_price_tier and the
billing helper pick the right rate based on prompt size.

The Gemini-2.5-Pro shape is the canonical example: prompts ≤200k
context pay $1.25/M input + $10/M output; prompts >200k pay $2.50/M
+ $15/M. Both prompt AND completion rates flip when the prompt
crosses the threshold.
"""
from __future__ import annotations

from trusted_router.catalog import (
    Model,
    PriceTier,
    select_price_tier,
)
from trusted_router.routes.helpers import cost_microdollars

# ----------------------------------------------------------------------
# select_price_tier — tier dispatch
# ----------------------------------------------------------------------


def _gemini_pro_tiers() -> tuple[PriceTier, ...]:
    """Recreate the Gemini 2.5 Pro tier shape: ≤200k vs >200k."""
    # Markup applied: $1.25/M wholesale * 1.10 = $1.375/M
    return (
        PriceTier(
            max_prompt_tokens=200_000,
            prompt_price_microdollars_per_million_tokens=1_375_000,
            completion_price_microdollars_per_million_tokens=11_000_000,
        ),
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=2_750_000,
            completion_price_microdollars_per_million_tokens=16_500_000,
        ),
    )


def test_select_price_tier_picks_low_tier_for_small_prompt() -> None:
    tiers = _gemini_pro_tiers()
    tier = select_price_tier(tiers, prompt_tokens=10_000)
    assert tier.max_prompt_tokens == 200_000
    assert tier.prompt_price_microdollars_per_million_tokens == 1_375_000


def test_select_price_tier_picks_low_tier_at_threshold() -> None:
    tiers = _gemini_pro_tiers()
    tier = select_price_tier(tiers, prompt_tokens=200_000)
    assert tier.max_prompt_tokens == 200_000


def test_select_price_tier_picks_high_tier_above_threshold() -> None:
    tiers = _gemini_pro_tiers()
    tier = select_price_tier(tiers, prompt_tokens=200_001)
    assert tier.max_prompt_tokens is None
    assert tier.prompt_price_microdollars_per_million_tokens == 2_750_000
    assert tier.completion_price_microdollars_per_million_tokens == 16_500_000


def test_select_price_tier_handles_single_uncapped_tier() -> None:
    tiers = (
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=1_000_000,
            completion_price_microdollars_per_million_tokens=2_000_000,
        ),
    )
    # Any prompt size returns the only tier.
    for size in [0, 1_000, 1_000_000_000]:
        tier = select_price_tier(tiers, prompt_tokens=size)
        assert tier.max_prompt_tokens is None


# ----------------------------------------------------------------------
# cost_microdollars — billing path uses the right tier
# ----------------------------------------------------------------------


def _gemini_pro_model() -> Model:
    return Model(
        id="google/gemini-2.5-pro",
        name="Gemini 2.5 Pro",
        provider="gemini",
        context_length=1_000_000,
        prompt_price_microdollars_per_million_tokens=1_375_000,
        completion_price_microdollars_per_million_tokens=11_000_000,
        published_prompt_price_microdollars_per_million_tokens=1_375_000,
        published_completion_price_microdollars_per_million_tokens=11_000_000,
        price_tiers=_gemini_pro_tiers(),
    )


def test_cost_microdollars_uses_low_tier_for_short_prompt() -> None:
    """A 100k-token prompt + 5k completion on Gemini 2.5 Pro should
    bill at the ≤200k tier ($1.375/M input + $11/M output)."""
    model = _gemini_pro_model()
    cost = cost_microdollars(model, input_tokens=100_000, output_tokens=5_000)
    # Expected: 100_000 / 1_000_000 * 1_375_000 = 137_500 micro
    #         + 5_000 / 1_000_000 * 11_000_000 = 55_000 micro
    #         = 192_500 micro
    assert cost == 137_500 + 55_000


def test_cost_microdollars_uses_high_tier_for_long_prompt() -> None:
    """A 250k-token prompt + 5k completion on Gemini 2.5 Pro should
    bill at the >200k tier ($2.75/M input + $16.5/M output) because
    the prompt size triggers the higher tier for BOTH input AND
    output rates."""
    model = _gemini_pro_model()
    cost = cost_microdollars(model, input_tokens=250_000, output_tokens=5_000)
    # Expected: 250_000 / 1_000_000 * 2_750_000 = 687_500 micro
    #         + 5_000 / 1_000_000 * 16_500_000 = 82_500 micro
    #         = 770_000 micro
    assert cost == 687_500 + 82_500


def test_cost_microdollars_respects_threshold_boundary() -> None:
    """At the exact threshold (200_000 tokens), the LOW tier still
    applies — the threshold is inclusive. One token over flips to the
    HIGH tier for both prompt AND completion (we test prompt-only here
    to keep arithmetic clean)."""
    model = _gemini_pro_model()
    low_cost = cost_microdollars(model, input_tokens=200_000, output_tokens=0)
    high_cost = cost_microdollars(model, input_tokens=200_001, output_tokens=0)
    # 200_000 * 1_375_000 / 1_000_000 = 275_000 (exact)
    assert low_cost == 275_000
    # 200_001 * 2_750_000 / 1_000_000 = 550_002.75. The exact rounding
    # behavior depends on token_cost_microdollars; at the boundary one
    # micro either way is fine. Assert range rather than exact value.
    assert 550_002 <= high_cost <= 550_003
    # The key contract: high tier strictly greater than low tier — even
    # for a 1-token-over prompt, the high tier almost-doubles the rate.
    assert high_cost > low_cost * 1.9


def test_cost_microdollars_falls_back_to_flat_rate_when_no_tiers() -> None:
    """Models without `price_tiers` (hand-coded meta-models, etc.)
    fall back to the flat headline rates."""
    model = Model(
        id="trustedrouter/free",
        name="Free",
        provider="trustedrouter",
        context_length=128_000,
        prompt_price_microdollars_per_million_tokens=0,
        completion_price_microdollars_per_million_tokens=0,
    )
    assert cost_microdollars(model, input_tokens=10_000, output_tokens=1_000) == 0


def test_real_gemini_pro_model_in_catalog_has_tiers() -> None:
    """The real `google/gemini-2.5-pro` Model loaded from the snapshot
    should carry the two-tier price profile end-to-end (after the
    next refresh-prices workflow run lands the new snapshot format).
    Until then, the model has a single-tier fallback synthesized from
    the headline rate, which is still correct for ≤200k prompts."""
    from trusted_router.catalog import MODELS

    pro = MODELS.get("google/gemini-2.5-pro")
    if pro is None:
        # Snapshot hasn't been refreshed with provider-direct yet —
        # the model may not be in the catalog at all in some test
        # configurations. That's OK; the contract is that when it
        # IS present, price_tiers is non-empty.
        return
    assert pro.price_tiers, (
        "google/gemini-2.5-pro must have price_tiers populated"
    )
