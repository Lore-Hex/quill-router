"""OpenAI request service tiers and their customer-visible prices.

The provider catalog has one OpenAI endpoint per usage type, while OpenAI
selects Standard versus Priority processing per request. Keep that second
pricing dimension here so authorization, settlement, and public endpoint
metadata use the same values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import CACHE_WRITE_1H_MULTIPLIER, _customer_price
from trusted_router.provider_lifecycle import openai_gpt56_sol_pricing_period

OPENAI_SERVICE_TIERS = ("default", "auto", "priority")
OPENAI_PRIORITY_MAX_PROMPT_TOKENS = 272_000


@dataclass(frozen=True)
class OpenAIPriorityPricing:
    prompt_microdollars_per_million_tokens: int
    cached_prompt_microdollars_per_million_tokens: int
    completion_microdollars_per_million_tokens: int
    cache_write_microdollars_per_million_tokens: int

    def public_payload(self) -> dict[str, int]:
        return {
            "prompt_microdollars_per_million_tokens": (self.prompt_microdollars_per_million_tokens),
            "cached_prompt_microdollars_per_million_tokens": (
                self.cached_prompt_microdollars_per_million_tokens
            ),
            "completion_microdollars_per_million_tokens": (
                self.completion_microdollars_per_million_tokens
            ),
            "max_prompt_tokens": OPENAI_PRIORITY_MAX_PROMPT_TOKENS,
        }


def _customer_priority_pricing(
    prompt_upstream_microdollars: int,
    cached_prompt_upstream_microdollars: int,
    completion_upstream_microdollars: int,
) -> OpenAIPriorityPricing:
    return OpenAIPriorityPricing(
        prompt_microdollars_per_million_tokens=_customer_price(prompt_upstream_microdollars),
        cached_prompt_microdollars_per_million_tokens=_customer_price(
            cached_prompt_upstream_microdollars
        ),
        completion_microdollars_per_million_tokens=_customer_price(
            completion_upstream_microdollars
        ),
        cache_write_microdollars_per_million_tokens=_customer_price(
            prompt_upstream_microdollars * 5 // 4
        ),
    )


_OPENAI_GPT56_SOL_MODEL_ID = "openai/gpt-5.6-sol"
# GPT-5.6 Sol's Fast (ex-Priority) rates share the Standard tier's promotion
# and switch with it at OPENAI_GPT56_SOL_STANDARD_PRICING_EFFECTIVE_AT, locked
# at authorization like every other effective-dated price. The public pricing
# page's Fast table (developers.openai.com/api/docs/pricing.md, read
# 2026-11-23) lists $8 input / $0.80 cached / $40 output per million up to
# 272K input during the promotion, 2x the $4 / $0.40 / $20 Standard
# promotion. After it, 2x Standard ($5 / $0.50 / $30) is $10 / $1 / $60.
_OPENAI_GPT56_SOL_FAST_PRICING: dict[str, OpenAIPriorityPricing] = {
    "promotion": _customer_priority_pricing(
        8_000_000,
        800_000,
        40_000_000,
    ),
    "standard": _customer_priority_pricing(
        10_000_000,
        1_000_000,
        60_000_000,
    ),
}

# Models whose Fast price does not change over time. GPT-5.6 Sol is priced by
# date above.
OPENAI_PRIORITY_PRICING: dict[str, OpenAIPriorityPricing] = {
    "openai/gpt-5.6-terra": _customer_priority_pricing(
        5_000_000,
        500_000,
        30_000_000,
    ),
    "openai/gpt-5.6-luna": _customer_priority_pricing(
        2_000_000,
        200_000,
        12_000_000,
    ),
}


def openai_priority_pricing(
    model_id: str,
    *,
    at: datetime | str | None = None,
) -> OpenAIPriorityPricing | None:
    """Fast (Priority) customer rates at ``at``: the authorization time when
    pricing a request, or the lifecycle clock when omitted."""
    if model_id == _OPENAI_GPT56_SOL_MODEL_ID:
        return _OPENAI_GPT56_SOL_FAST_PRICING[openai_gpt56_sol_pricing_period(at)]
    return OPENAI_PRIORITY_PRICING.get(model_id)


def openai_priority_cost_microdollars(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    effective_at: datetime | str | None = None,
) -> int:
    pricing = openai_priority_pricing(model_id, at=effective_at)
    if pricing is None:
        raise ValueError(f"OpenAI Priority processing is unavailable for {model_id}")
    creation_1h = min(max(cache_creation_1h_tokens, 0), max(cache_creation_tokens, 0))
    cost = (
        token_cost_microdollars(
            input_tokens,
            pricing.prompt_microdollars_per_million_tokens,
        )
        + token_cost_microdollars(
            cache_read_tokens,
            pricing.cached_prompt_microdollars_per_million_tokens,
        )
        + token_cost_microdollars(
            cache_creation_tokens - creation_1h,
            pricing.cache_write_microdollars_per_million_tokens,
        )
        + token_cost_microdollars(
            creation_1h,
            CACHE_WRITE_1H_MULTIPLIER * pricing.prompt_microdollars_per_million_tokens,
        )
        + token_cost_microdollars(
            output_tokens,
            pricing.completion_microdollars_per_million_tokens,
        )
    )
    has_positive_charge = any(
        count > 0
        for count in (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
        )
    )
    return max(cost, 1) if has_positive_charge else 0
