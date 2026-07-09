"""Customer-facing pricing math for the model catalog.

Extracted from catalog.py (#38): the markup + floor, per-token cost, cache-token
pricing, price-tier selection, and provider-manifest price parsing. Pure
functions of the money primitives — NO dependency on the catalog data
(PROVIDERS/MODELS) — so a pricing change is reviewable in isolation from the
catalog. catalog.py re-exports these for backward compatibility.

Request cost callers intentionally differ only in cache policy; tier selection
and prompt/completion rate resolution must go through resolve_request_rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict

from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    TOKENS_PER_MILLION,
    dollars_to_microdollars,
)


@dataclass(frozen=True)
class PriceTier:
    """One tier of context-conditional pricing. A request whose prompt
    token count is ≤ `max_prompt_tokens` uses this tier's rates. The
    LAST tier in `Model.price_tiers` MUST have `max_prompt_tokens=None`
    (uncapped fallback). Most models have exactly one tier.

    Both prompt and completion rates live on the tier — Gemini-Pro-shape
    pricing flips both rates when context crosses 200k tokens.

    `prompt_cached_*` is the discounted rate for prompt tokens that
    upstream reports as cache hits. None ⇒ upstream charges the same
    rate cached or not (rare; most providers offer a cache discount).
    Per-token billing splits the prompt into (uncached × full rate) +
    (cached × cached rate); see `cost_microdollars` in routes/helpers.
    """

    max_prompt_tokens: int | None
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    prompt_cached_price_microdollars_per_million_tokens: int | None = None


@dataclass(frozen=True)
class RequestRates:
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    # Tier-declared cached-read rate; None when the selected tier declares none.
    prompt_cached_price_microdollars_per_million_tokens: int | None


def _flat_tier(
    prompt: int,
    completion: int,
    prompt_cached: int | None = None,
) -> tuple[PriceTier, ...]:
    """Construct a length-1 tier tuple (the common case)."""
    return (
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=prompt,
            completion_price_microdollars_per_million_tokens=completion,
            prompt_cached_price_microdollars_per_million_tokens=prompt_cached,
        ),
    )

def select_price_tier(tiers: tuple[PriceTier, ...], prompt_tokens: int) -> PriceTier:
    """Pick the tier that applies to a request with `prompt_tokens` of
    input. Walks the tiers in order; returns the first one whose
    threshold accommodates the prompt size. The last tier always has
    max_prompt_tokens=None and is the catch-all.

    Used by the billing path to compute actual cost. For models with
    a single uncapped tier (the common case), this returns that tier
    regardless of `prompt_tokens`.
    """
    for tier in tiers:
        if tier.max_prompt_tokens is None or prompt_tokens <= tier.max_prompt_tokens:
            return tier
    # Should be unreachable — the last tier always matches due to
    # max_prompt_tokens=None — but defend against malformed catalog data.
    return tiers[-1]


def resolve_request_rates(
    tiers: tuple[PriceTier, ...],
    *,
    headline_prompt_micro_per_m: int,
    headline_completion_micro_per_m: int,
    total_prompt_tokens: int,
) -> RequestRates:
    if tiers:
        tier = select_price_tier(tiers, total_prompt_tokens)
        return RequestRates(
            prompt_price_microdollars_per_million_tokens=(
                tier.prompt_price_microdollars_per_million_tokens
            ),
            completion_price_microdollars_per_million_tokens=(
                tier.completion_price_microdollars_per_million_tokens
            ),
            prompt_cached_price_microdollars_per_million_tokens=(
                tier.prompt_cached_price_microdollars_per_million_tokens
            ),
        )
    return RequestRates(
        prompt_price_microdollars_per_million_tokens=headline_prompt_micro_per_m,
        completion_price_microdollars_per_million_tokens=headline_completion_micro_per_m,
        prompt_cached_price_microdollars_per_million_tokens=None,
    )


class ModelPricingKwargs(TypedDict):
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    published_prompt_price_microdollars_per_million_tokens: int
    published_completion_price_microdollars_per_million_tokens: int

_PRICE_MARKUP_RATIO = Decimal("1.10")

_PRICE_FLOOR_MICRODOLLARS_PER_M = 10_000  # $0.01 per million tokens.

def _customer_price(cost_microdollars_per_million: int) -> int:
    """Apply the markup formula. Input/output in microdollars per million tokens."""
    marked_up = int(
        (Decimal(cost_microdollars_per_million) * _PRICE_MARKUP_RATIO).to_integral_value()
    )
    return max(marked_up, _PRICE_FLOOR_MICRODOLLARS_PER_M)

_CACHE_READ_PRICE_MULTIPLIER: dict[str, Decimal] = {
    "anthropic": Decimal("0.1"),
    "openai": Decimal("0.5"),
    "gemini": Decimal("0.25"),
    "vertex": Decimal("0.25"),
}

_CACHE_WRITE_PRICE_MULTIPLIER: dict[str, Decimal] = {
    "anthropic": Decimal("1.25"),
}

_DEFAULT_CACHE_READ_MULTIPLIER = Decimal("1")

_DEFAULT_CACHE_WRITE_MULTIPLIER = Decimal("1.25")

def cache_token_prices_microdollars(
    provider: str, prompt_price_microdollars: int
) -> tuple[int, int]:
    """(cache-read, cache-write) customer price in microdollars per million
    tokens for one endpoint's prompt price."""
    prompt = Decimal(prompt_price_microdollars)
    read = _CACHE_READ_PRICE_MULTIPLIER.get(provider, _DEFAULT_CACHE_READ_MULTIPLIER)
    write = _CACHE_WRITE_PRICE_MULTIPLIER.get(provider, _DEFAULT_CACHE_WRITE_MULTIPLIER)
    return (
        int((prompt * read).to_integral_value()),
        int((prompt * write).to_integral_value()),
    )

def _priced(cost_dollars_per_million: str | int | float) -> tuple[int, int, int]:
    """Return (prompt_price, published_price, cost_microdollars) for a
    dollars-per-million cost. prompt_price == published_price under the
    uniform formula; cost is preserved as a third value for any consumer
    that wants the upstream-paid amount (e.g. the per-endpoint detail page)."""
    cost = dollars_to_microdollars(cost_dollars_per_million)
    customer = _customer_price(cost)
    return customer, customer, cost

def _customer_price_from_dollars_per_token(price_per_token: str) -> tuple[int, int, int]:
    """Variant for snapshot-shaped inputs (dollars/token strings).
    Returns the same triple as `_priced`."""
    if not price_per_token:
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    try:
        per_token = Decimal(str(price_per_token))
    except (InvalidOperation, ValueError):
        # Malformed snapshot rows are pinned to the price floor — better
        # to advertise $0.01/M than to crash module import or expose $0.
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    cost = int((per_token * MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION).to_integral_value())
    customer = _customer_price(cost)
    return customer, customer, cost

def _read_pricing_tiers(pricing: dict[str, Any], dimension: str) -> tuple[PriceTier, ...] | None:
    """Read `pricing.prompt_tiers` / `pricing.completion_tiers` arrays
    from the snapshot. Returns None if the snapshot has only flat
    pricing for this model — caller should construct a single-tier
    list from the headline rate in that case.

    Tier shape in the snapshot:
        prompt_tiers:     [{"max_prompt_tokens": int|None,
                            "prompt": "$/tok",
                            "input_cache_read": "$/tok"  # optional}]
        completion_tiers: [{"max_prompt_tokens": int|None, "completion": "$/tok"}]

    Both arrays have the same length and same `max_prompt_tokens`
    sequence. Returned PriceTier objects pair them up; cached prompt
    rate is parsed from `input_cache_read` (matches OR's convention).
    """
    raw_prompt = pricing.get("prompt_tiers")
    raw_completion = pricing.get("completion_tiers")
    if not isinstance(raw_prompt, list) or not isinstance(raw_completion, list):
        return None
    if not raw_prompt or len(raw_prompt) != len(raw_completion):
        return None
    tiers: list[PriceTier] = []
    for prompt_tier, completion_tier in zip(raw_prompt, raw_completion, strict=False):
        if not isinstance(prompt_tier, dict) or not isinstance(completion_tier, dict):
            return None
        threshold = prompt_tier.get("max_prompt_tokens")
        if threshold is not None and not isinstance(threshold, int):
            return None
        prompt_per_token = str(prompt_tier.get("prompt") or "")
        completion_per_token = str(completion_tier.get("completion") or "")
        prompt_micro, _pub, _cost = _customer_price_from_dollars_per_token(prompt_per_token)
        completion_micro, _pub2, _cost2 = _customer_price_from_dollars_per_token(
            completion_per_token
        )
        cached_micro: int | None = None
        cache_read = prompt_tier.get("input_cache_read")
        if cache_read:
            cached_micro, _pub3, _cost3 = _customer_price_from_dollars_per_token(str(cache_read))
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_price_microdollars_per_million_tokens=prompt_micro,
                completion_price_microdollars_per_million_tokens=completion_micro,
                prompt_cached_price_microdollars_per_million_tokens=cached_micro,
            )
        )
    if tiers[-1].max_prompt_tokens is not None:
        # Snapshot data is malformed — last tier should be uncapped.
        # Return None so caller falls back to the headline rate.
        return None
    return tuple(tiers)

def _as_positive_int(value: object) -> int:
    if not isinstance(value, int | str | float | bytes | bytearray):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)

def _provider_manifest_price_scale(raw: dict[str, Any]) -> int:
    """Return the multiplier needed to turn provider-manifest price fields
    into microdollars per million tokens.

    Most manifests store true microdollars/M. Novita's `/models` feed stores
    prices 100x smaller than its public `$ /Mt` table, so its manifest carries
    an explicit scale to prevent the catalog from falling through to the
    global $0.01/M floor.
    """
    scale = _as_positive_int(raw.get("price_scale_to_microdollars_per_million_tokens"))
    return max(scale, 1)

def _provider_manifest_price_cost(value: object, *, price_scale: int) -> int:
    parsed = _as_positive_int(value)
    if parsed <= 0:
        return 0
    return parsed * price_scale

def _provider_manifest_price_tiers(
    raw_model: dict[str, Any],
    default_prompt_price: int,
    default_completion_price: int,
    default_cached_prompt_price: int | None,
    *,
    price_scale: int = 1,
) -> tuple[PriceTier, ...]:
    raw_tiers = raw_model.get("price_tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        return _flat_tier(
            default_prompt_price,
            default_completion_price,
            prompt_cached=default_cached_prompt_price,
        )

    tiers: list[PriceTier] = []
    for raw_tier in raw_tiers:
        if not isinstance(raw_tier, dict):
            return _flat_tier(
                default_prompt_price,
                default_completion_price,
                prompt_cached=default_cached_prompt_price,
            )
        raw_threshold = raw_tier.get("max_prompt_tokens")
        if raw_threshold is None:
            threshold = None
        elif isinstance(raw_threshold, int | str | float | bytes | bytearray):
            threshold = _as_positive_int(raw_threshold)
            if threshold <= 0:
                return _flat_tier(
                    default_prompt_price,
                    default_completion_price,
                    prompt_cached=default_cached_prompt_price,
                )
        else:
            return _flat_tier(
                default_prompt_price,
                default_completion_price,
                prompt_cached=default_cached_prompt_price,
            )

        prompt_cost = _provider_manifest_price_cost(
            raw_tier.get("input_token_price_per_m"),
            price_scale=price_scale,
        )
        completion_cost = _provider_manifest_price_cost(
            raw_tier.get("output_token_price_per_m"),
            price_scale=price_scale,
        )
        if prompt_cost <= 0 or completion_cost <= 0:
            return _flat_tier(
                default_prompt_price,
                default_completion_price,
                prompt_cached=default_cached_prompt_price,
            )
        cached_cost = _provider_manifest_price_cost(
            raw_tier.get("cached_input_token_price_per_m"),
            price_scale=price_scale,
        )
        cached_price = _customer_price(cached_cost) if cached_cost > 0 else None
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_price_microdollars_per_million_tokens=_customer_price(prompt_cost),
                completion_price_microdollars_per_million_tokens=_customer_price(completion_cost),
                prompt_cached_price_microdollars_per_million_tokens=cached_price,
            )
        )

    if tiers[-1].max_prompt_tokens is not None:
        return _flat_tier(
            default_prompt_price,
            default_completion_price,
            prompt_cached=default_cached_prompt_price,
        )
    return tuple(tiers)
