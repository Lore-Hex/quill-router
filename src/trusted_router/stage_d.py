"""Stage D pricing snapshots and document-driven endpoint billing.

The snapshot deliberately contains only integer money inputs consumed by the
standard endpoint pricing path.  Once an authorization is admitted to the
Stage D cohort, heartbeat pricing never consults the mutable live catalog.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from trusted_router.catalog import cache_token_prices_microdollars
from trusted_router.money import token_cost_microdollars

PRICING_DOCUMENT_VERSION = 1
PRICE_HISTORY_VERSION = 1
PRICING_ROUNDING = "half_up_per_million"


def endpoint_pricing_candidate(endpoint: Any) -> dict[str, Any]:
    """Freeze every standard-pricing input for one effective endpoint."""

    def rates(
        prompt_micro: int,
        output_micro: int,
        cached_micro: int | None,
    ) -> dict[str, int]:
        default_cached, cache_creation = cache_token_prices_microdollars(
            str(endpoint.provider), int(prompt_micro)
        )
        return {
            "input_micro_per_million": int(prompt_micro),
            "output_micro_per_million": int(output_micro),
            "cached_input_micro_per_million": int(
                default_cached if cached_micro is None else cached_micro
            ),
            "cache_creation_micro_per_million": int(cache_creation),
        }

    return {
        "endpoint_id": str(endpoint.id),
        "price_history_version": PRICE_HISTORY_VERSION,
        "rates": rates(
            endpoint.prompt_price_microdollars_per_million_tokens,
            endpoint.completion_price_microdollars_per_million_tokens,
            None,
        ),
        "tiers": [
            {
                "max_prompt_tokens": tier.max_prompt_tokens,
                "rates": rates(
                    tier.prompt_price_microdollars_per_million_tokens,
                    tier.completion_price_microdollars_per_million_tokens,
                    tier.prompt_cached_price_microdollars_per_million_tokens,
                ),
            }
            for tier in (getattr(endpoint, "price_tiers", ()) or ())
        ],
        "request_fee_micro": int(endpoint.request_price_microdollars),
        "rounding": PRICING_ROUNDING,
    }


def endpoint_pricing_document(endpoints: Iterable[Any]) -> dict[str, Any]:
    return {
        "v": PRICING_DOCUMENT_VERSION,
        "kind": "endpoint",
        "candidates": [endpoint_pricing_candidate(endpoint) for endpoint in endpoints],
    }


def canonical_pricing_snapshot(document: Mapping[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def parse_pricing_snapshot(snapshot: str) -> dict[str, Any]:
    document = json.loads(snapshot)
    if document.get("v") != PRICING_DOCUMENT_VERSION or document.get("kind") != "endpoint":
        raise ValueError("unsupported Stage D pricing document")
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Stage D pricing document has no candidates")
    return document


def pricing_candidate_for_endpoint(
    document: Mapping[str, Any], endpoint_id: str
) -> Mapping[str, Any]:
    candidates = document.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Stage D pricing document has malformed candidates")
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate.get("endpoint_id") == endpoint_id:
            return candidate
    raise ValueError("selected endpoint is absent from the Stage D pricing document")


def endpoint_cost_microdollars_from_candidate(
    candidate: Mapping[str, Any],
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    price_tier_input_tokens: int | None = None,
) -> int:
    """Price usage using a frozen candidate as the only pricing input."""

    if candidate.get("rounding") != PRICING_ROUNDING:
        raise ValueError("unsupported Stage D pricing rounding")
    total_prompt = int(input_tokens) + int(cache_read_tokens) + int(cache_creation_tokens)
    tier_prompt = total_prompt
    if (
        price_tier_input_tokens is not None
        and int(price_tier_input_tokens) > 0
        and int(price_tier_input_tokens) <= total_prompt
    ):
        tier_prompt = int(price_tier_input_tokens)

    selected_rates = candidate.get("rates")
    tiers = candidate.get("tiers")
    if isinstance(tiers, list) and tiers:
        selected_rates = None
        for tier in tiers:
            if not isinstance(tier, Mapping):
                raise ValueError("malformed Stage D pricing tier")
            maximum = tier.get("max_prompt_tokens")
            if maximum is None or tier_prompt <= int(maximum):
                selected_rates = tier.get("rates")
                break
        if selected_rates is None:
            selected_rates = tiers[-1].get("rates")
    if not isinstance(selected_rates, Mapping):
        raise ValueError("malformed Stage D pricing rates")

    request_fee = int(candidate.get("request_fee_micro") or 0)
    input_rate = int(selected_rates["input_micro_per_million"])
    output_rate = int(selected_rates["output_micro_per_million"])
    cached_rate = int(selected_rates["cached_input_micro_per_million"])
    creation_rate = int(selected_rates["cache_creation_micro_per_million"])
    cost = (
        request_fee
        + token_cost_microdollars(input_tokens, input_rate)
        + token_cost_microdollars(output_tokens, output_rate)
        + token_cost_microdollars(cache_read_tokens, cached_rate)
        + token_cost_microdollars(cache_creation_tokens, creation_rate)
    )
    positive = (
        request_fee > 0
        or (input_tokens > 0 and input_rate > 0)
        or (output_tokens > 0 and output_rate > 0)
        or (cache_read_tokens > 0 and cached_rate > 0)
        or (cache_creation_tokens > 0 and creation_rate > 0)
    )
    return max(cost, 1) if positive else 0


def endpoint_cost_microdollars_from_document(
    document: Mapping[str, Any],
    endpoint_id: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    price_tier_input_tokens: int | None = None,
) -> int:
    return endpoint_cost_microdollars_from_candidate(
        pricing_candidate_for_endpoint(document, endpoint_id),
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        price_tier_input_tokens=price_tier_input_tokens,
    )
