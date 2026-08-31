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
from decimal import ROUND_CEILING, Decimal, InvalidOperation
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


STANDARD_PRICE_MARKUP_BASIS_POINTS = 550
SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS = 1_200

_PRICE_MARKUP_RATIO = Decimal(10_000 + STANDARD_PRICE_MARKUP_BASIS_POINTS) / Decimal(10_000)

_PRICE_FLOOR_MICRODOLLARS_PER_M = 10_000  # $0.01 per million tokens.


def _customer_price(cost_microdollars_per_million: int) -> int:
    """Apply the markup formula. Input/output in microdollars per million tokens."""
    marked_up = int(
        (Decimal(cost_microdollars_per_million) * _PRICE_MARKUP_RATIO).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return max(marked_up, _PRICE_FLOOR_MICRODOLLARS_PER_M)


def _provider_manifest_customer_price(
    cost_microdollars_per_million: int,
    *,
    apply_markup: bool,
) -> int:
    """Convert manifest cost to retail without rewriting the source cost."""

    if not apply_markup:
        return cost_microdollars_per_million
    return _customer_price(cost_microdollars_per_million)


def customer_fixed_price_microdollars(cost_microdollars: int) -> int:
    """Apply the standard markup to a fixed provider charge.

    Fixed request and media charges are already expressed in the ledger's
    microdollar unit, so they must not inherit the per-million-token floor.
    """

    if isinstance(cost_microdollars, bool) or not isinstance(cost_microdollars, int):
        raise ValueError("fixed provider price must be a non-negative integer")
    if cost_microdollars < 0:
        raise ValueError("fixed provider price must be a non-negative integer")
    return int(
        (Decimal(cost_microdollars) * _PRICE_MARKUP_RATIO).to_integral_value(rounding=ROUND_CEILING)
    )


def signed_receipt_price_microdollars(
    standard_price_microdollars: int,
    total_fee_basis_points: int = SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS,
) -> int:
    """Upgrade a standard retail charge to the signed-receipt total fee.

    Catalog prices already include TrustedRouter's standard 5.5% fee. Receipt
    billing therefore scales the frozen retail charge from 105.5% to 112%,
    rather than adding another 12%. Integer ceiling keeps every positive
    premium representable in the microdollar ledger.
    """

    if isinstance(standard_price_microdollars, bool) or not isinstance(
        standard_price_microdollars, int
    ):
        raise ValueError("standard price must be a non-negative integer")
    if standard_price_microdollars < 0:
        raise ValueError("standard price must be a non-negative integer")
    if isinstance(total_fee_basis_points, bool) or not isinstance(total_fee_basis_points, int):
        raise ValueError("total fee basis points must be an integer")
    if total_fee_basis_points < STANDARD_PRICE_MARKUP_BASIS_POINTS:
        raise ValueError("total fee cannot be below the standard fee")
    if standard_price_microdollars == 0:
        return 0
    numerator = 10_000 + total_fee_basis_points
    denominator = 10_000 + STANDARD_PRICE_MARKUP_BASIS_POINTS
    return (standard_price_microdollars * numerator + denominator - 1) // denominator


_CACHE_READ_PRICE_MULTIPLIER: dict[str, Decimal] = {
    "anthropic": Decimal("0.1"),
    "openai": Decimal("0.5"),
    "gemini": Decimal("0.25"),  # pre-split settlement compatibility
    "google-ai-studio": Decimal("0.25"),
    "google-vertex": Decimal("0.25"),
    "vertex": Decimal("0.25"),
    # The entries below are fallbacks for providers with a CONFIRMED uniform
    # published cache-read policy, used only when the endpoint carries no
    # per-model cached price (which always wins — see the settle path).
    # Do NOT add a provider here from a single model's ratio: providers
    # without a uniform policy (deepseek, moonshotai, z-ai, deepinfra) price
    # cache hits per model and are covered by manifest/parser prices instead.
    # mistral.ai/pricing: "cached input tokens reduce input cost by up to
    # 90% for repeated prompts" — flat 90% discount (verified 2026-08-31).
    "mistral": Decimal("0.1"),
    # Fireworks documents an automatic 50% cached-prompt discount across
    # serverless models (verified 2026-08-31).
    "fireworks": Decimal("0.5"),
    # Alibaba Model Studio implicit context cache bills hits at 20% of the
    # standard input price (verified 2026-08-31).
    "alibaba": Decimal("0.2"),
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


def _optional_customer_price_from_dollars_per_token(value: object) -> int | None:
    """Parse an optional cached-input price without inventing a discount.

    A published zero is valid and receives the normal customer price floor.
    Missing, negative, non-finite, or malformed values mean the provider did
    not publish a cache-read rate, so callers must bill cached tokens at the
    regular prompt rate.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        per_token = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not per_token.is_finite() or per_token < 0:
        return None
    cost = int((per_token * MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION).to_integral_value())
    return _customer_price(cost)


def _strict_customer_price_from_dollars_per_token(
    value: object,
    *,
    allow_zero: bool = False,
) -> int:
    """Parse an exact tier price or reject the complete tiered route."""

    try:
        per_token = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("malformed tier price") from None
    if not per_token.is_finite() or per_token < 0 or (not allow_zero and per_token == 0):
        raise ValueError("malformed tier price")
    cost = int((per_token * MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION).to_integral_value())
    return _customer_price(cost)


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
    has_prompt = "prompt_tiers" in pricing
    has_completion = "completion_tiers" in pricing
    if not has_prompt and not has_completion:
        return None
    if not isinstance(raw_prompt, list) or not isinstance(raw_completion, list):
        raise ValueError(f"malformed {dimension} pricing tiers")
    if not raw_prompt or len(raw_prompt) != len(raw_completion):
        raise ValueError(f"malformed {dimension} pricing tiers")
    tiers: list[PriceTier] = []
    previous_threshold = 0
    previous_prompt_price = 0
    previous_completion_price = 0
    previous_cached_price: int | None = None
    for index, (prompt_tier, completion_tier) in enumerate(
        zip(raw_prompt, raw_completion, strict=True)
    ):
        if not isinstance(prompt_tier, dict) or not isinstance(completion_tier, dict):
            raise ValueError(f"malformed {dimension} pricing tiers")
        threshold = prompt_tier.get("max_prompt_tokens")
        completion_threshold = completion_tier.get("max_prompt_tokens")
        if isinstance(completion_threshold, bool) or completion_threshold != threshold:
            raise ValueError(f"mismatched {dimension} pricing thresholds")
        if threshold is None:
            if index != len(raw_prompt) - 1:
                raise ValueError(f"uncapped {dimension} pricing tier must be last")
        elif isinstance(threshold, bool) or not isinstance(threshold, int):
            raise ValueError(f"malformed {dimension} pricing threshold")
        elif threshold <= previous_threshold:
            raise ValueError(f"unordered {dimension} pricing thresholds")
        else:
            previous_threshold = threshold

        try:
            prompt_micro = _strict_customer_price_from_dollars_per_token(prompt_tier.get("prompt"))
            completion_micro = _strict_customer_price_from_dollars_per_token(
                completion_tier.get("completion")
            )
            cached_micro = (
                _strict_customer_price_from_dollars_per_token(
                    prompt_tier.get("input_cache_read"),
                    allow_zero=True,
                )
                if "input_cache_read" in prompt_tier
                else None
            )
        except ValueError as exc:
            raise ValueError(f"malformed {dimension} tier price") from exc
        if (
            prompt_micro < previous_prompt_price
            or completion_micro < previous_completion_price
            or (
                cached_micro is not None
                and previous_cached_price is not None
                and cached_micro < previous_cached_price
            )
        ):
            raise ValueError(f"decreasing {dimension} tier price")
        previous_prompt_price = prompt_micro
        previous_completion_price = completion_micro
        if cached_micro is not None:
            previous_cached_price = cached_micro
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_price_microdollars_per_million_tokens=prompt_micro,
                completion_price_microdollars_per_million_tokens=completion_micro,
                prompt_cached_price_microdollars_per_million_tokens=cached_micro,
            )
        )
    if tiers[-1].max_prompt_tokens is not None:
        raise ValueError(f"capped final {dimension} pricing tier")
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


def _provider_manifest_optional_price_cost(
    value: object,
    *,
    price_scale: int,
) -> int | None:
    """Parse an optional manifest price while preserving explicit zero.

    Missing or malformed values return None so cached tokens use the regular
    prompt rate. A literal zero remains zero and receives the customer floor.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | str | float | bytes | bytearray):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0:
        return None
    return parsed * price_scale


def _provider_manifest_exact_integer(value: object) -> int | None:
    """Return an exact manifest integer without silently truncating floats."""

    if isinstance(value, bool) or not isinstance(value, int | str | float | bytes | bytearray):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def provider_manifest_price_tiers_are_valid(raw_tiers: object) -> bool:
    """Validate provider tier ordering and exact positive token prices.

    A malformed tier must never collapse to a cheaper headline rate. Provider
    manifests are an accounting input, so structural ambiguity is a quarantine
    condition rather than a best-effort parsing opportunity.
    """

    if not isinstance(raw_tiers, list) or not raw_tiers:
        return False
    previous_threshold = 0
    previous_prompt_price = 0
    previous_completion_price = 0
    previous_cached_price: int | None = None
    for index, raw_tier in enumerate(raw_tiers):
        if not isinstance(raw_tier, dict):
            return False
        threshold = raw_tier.get("max_prompt_tokens")
        if threshold is None:
            if index != len(raw_tiers) - 1:
                return False
        else:
            if isinstance(threshold, bool) or (
                isinstance(threshold, float) and not threshold.is_integer()
            ):
                return False
            parsed_threshold = _as_positive_int(threshold)
            if parsed_threshold <= previous_threshold:
                return False
            previous_threshold = parsed_threshold
        prompt = _provider_manifest_exact_integer(raw_tier.get("input_token_price_per_m"))
        completion = _provider_manifest_exact_integer(raw_tier.get("output_token_price_per_m"))
        if (
            prompt is None
            or completion is None
            or prompt <= 0
            or completion <= 0
            or prompt < previous_prompt_price
            or completion < previous_completion_price
        ):
            return False
        cached = None
        if "cached_input_token_price_per_m" in raw_tier:
            cached = _provider_manifest_exact_integer(
                raw_tier.get("cached_input_token_price_per_m")
            )
            if cached is None or cached < 0:
                return False
        if (
            cached is not None
            and previous_cached_price is not None
            and cached < previous_cached_price
        ):
            return False
        previous_prompt_price = prompt
        previous_completion_price = completion
        if cached is not None:
            previous_cached_price = cached
    return raw_tiers[-1].get("max_prompt_tokens") is None


def provider_manifest_price_profile_is_valid(raw_model: object) -> bool:
    """Validate flat prices and require tier zero to match the headline."""

    if not isinstance(raw_model, dict):
        return False
    prompt = _provider_manifest_exact_integer(raw_model.get("input_token_price_per_m"))
    completion = _provider_manifest_exact_integer(raw_model.get("output_token_price_per_m"))
    if prompt is None or completion is None or prompt <= 0 or completion <= 0:
        return False
    cached_present = "cached_input_token_price_per_m" in raw_model
    cached = (
        _provider_manifest_exact_integer(raw_model.get("cached_input_token_price_per_m"))
        if cached_present
        else None
    )
    if cached_present and (cached is None or cached < 0):
        return False
    if "price_tiers" not in raw_model:
        return True
    raw_tiers = raw_model.get("price_tiers")
    if not provider_manifest_price_tiers_are_valid(raw_tiers):
        return False
    assert isinstance(raw_tiers, list)
    first = raw_tiers[0]
    assert isinstance(first, dict)
    if (
        _provider_manifest_exact_integer(first.get("input_token_price_per_m")) != prompt
        or _provider_manifest_exact_integer(first.get("output_token_price_per_m")) != completion
    ):
        return False
    first_cached_present = "cached_input_token_price_per_m" in first
    first_cached = (
        _provider_manifest_exact_integer(first.get("cached_input_token_price_per_m"))
        if first_cached_present
        else None
    )
    # Some provider feeds omit the optional headline cache rate while giving
    # exact per-tier cache rates. That is safe because billing reads the tier;
    # when a headline cache rate is present, however, it must describe tier 0.
    return not cached_present or (first_cached_present and cached == first_cached)


def _provider_manifest_price_tiers(
    raw_model: dict[str, Any],
    default_prompt_price: int,
    default_completion_price: int,
    default_cached_prompt_price: int | None,
    *,
    price_scale: int = 1,
    apply_markup: bool = True,
) -> tuple[PriceTier, ...]:
    if "price_tiers" not in raw_model:
        return _flat_tier(
            default_prompt_price,
            default_completion_price,
            prompt_cached=default_cached_prompt_price,
        )
    raw_tiers = raw_model.get("price_tiers")
    if not provider_manifest_price_profile_is_valid(raw_model):
        raise ValueError("provider manifest has malformed or inconsistent price_tiers")
    assert isinstance(raw_tiers, list)

    tiers: list[PriceTier] = []
    for raw_tier in raw_tiers:
        assert isinstance(raw_tier, dict)
        raw_threshold = raw_tier.get("max_prompt_tokens")
        if raw_threshold is None:
            threshold = None
        else:
            threshold = _as_positive_int(raw_threshold)

        prompt_cost = _provider_manifest_price_cost(
            raw_tier.get("input_token_price_per_m"),
            price_scale=price_scale,
        )
        completion_cost = _provider_manifest_price_cost(
            raw_tier.get("output_token_price_per_m"),
            price_scale=price_scale,
        )
        cached_cost = _provider_manifest_optional_price_cost(
            raw_tier.get("cached_input_token_price_per_m"),
            price_scale=price_scale,
        )
        cached_price = (
            _provider_manifest_customer_price(cached_cost, apply_markup=apply_markup)
            if cached_cost is not None
            else None
        )
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_price_microdollars_per_million_tokens=(
                    _provider_manifest_customer_price(
                        prompt_cost,
                        apply_markup=apply_markup,
                    )
                ),
                completion_price_microdollars_per_million_tokens=(
                    _provider_manifest_customer_price(
                        completion_cost,
                        apply_markup=apply_markup,
                    )
                ),
                prompt_cached_price_microdollars_per_million_tokens=cached_price,
            )
        )

    return tuple(tiers)
