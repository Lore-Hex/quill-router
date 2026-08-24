"""OpenAI request service tiers and their customer-visible prices.

The provider catalog has one OpenAI endpoint per usage type, while OpenAI
selects Standard versus Priority processing per request. Keep that second
pricing dimension here so authorization, settlement, and public endpoint
metadata use the same values.
"""

from __future__ import annotations

from dataclasses import dataclass

from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import _customer_price

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


OPENAI_PRIORITY_PRICING: dict[str, OpenAIPriorityPricing] = {
    "openai/gpt-5.6-sol": _customer_priority_pricing(
        10_000_000,
        1_000_000,
        60_000_000,
    ),
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


def openai_priority_pricing(model_id: str) -> OpenAIPriorityPricing | None:
    return OPENAI_PRIORITY_PRICING.get(model_id)


def openai_priority_cost_microdollars(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    pricing = openai_priority_pricing(model_id)
    if pricing is None:
        raise ValueError(f"OpenAI Priority processing is unavailable for {model_id}")
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
            cache_creation_tokens,
            pricing.cache_write_microdollars_per_million_tokens,
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
