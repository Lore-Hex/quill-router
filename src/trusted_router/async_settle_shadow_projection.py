"""Frozen catalog projection cache; no catalog reloads or request-time I/O."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from trusted_router import billing_snapshot as b
from trusted_router.catalog import effective_endpoint
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.stage_d import endpoint_pricing_candidate


def _integer(value: Any) -> None:
    if type(value) is not int or not 0 <= value <= b.MAX_INT:
        raise ValueError("integer")


@lru_cache(maxsize=4096)
def candidate(endpoint: ModelEndpoint) -> b.Candidate:
    for value in (endpoint.prompt_price_microdollars_per_million_tokens,
                  endpoint.completion_price_microdollars_per_million_tokens,
                  endpoint.request_price_microdollars):
        _integer(value)
    for tier in endpoint.price_tiers:
        for tier_value in (tier.max_prompt_tokens, tier.prompt_price_microdollars_per_million_tokens,
                      tier.completion_price_microdollars_per_million_tokens,
                      tier.prompt_cached_price_microdollars_per_million_tokens):
            if tier_value is not None:
                _integer(tier_value)
    if (endpoint.provider not in {"openai", "anthropic"} or endpoint.usage_type != "Credits"
            or not endpoint.model_id.startswith(endpoint.provider + "/")
            or endpoint.request_price_microdollars != 0
            or any(re.fullmatch(r"[A-Za-z0-9_./:@+\-]{1,128}", identity) is None for identity in (endpoint.id, endpoint.model_id))):
        raise ValueError("unsupported candidate")
    price = endpoint_pricing_candidate(endpoint)
    def rates(value: dict[str, int]) -> b.Rates:
        for amount in value.values():
            _integer(amount)
        return b.Rates.model_construct(
            input_micro_per_million=value["input_micro_per_million"],
            output_micro_per_million=value["output_micro_per_million"],
            cached_input_micro_per_million=value["cached_input_micro_per_million"],
            cache_creation_micro_per_million=value["cache_creation_micro_per_million"])
    tiers = []
    previous = -1
    if len(price["tiers"]) > 64:
        raise ValueError("tiers")
    for index, tier in enumerate(price["tiers"]):
        maximum = tier["max_prompt_tokens"]
        if maximum is None:
            if index != len(price["tiers"]) - 1:
                raise ValueError("tiers")
        elif maximum <= previous:
            raise ValueError("tiers")
        else:
            previous = maximum
        tiers.append(b.Tier.model_construct(max_prompt_tokens=maximum, rates=rates(tier["rates"])))
    # This is a projection of immutable, server-owned catalog values. Validate
    # its complete finite domain once; never use model_construct on wire data.
    return b.Candidate.model_construct(endpoint_id=endpoint.id, provider=endpoint.provider,
        model_id=endpoint.model_id, usage_type="Credits", price_history_version=1,
        rates=rates(price["rates"]), tiers=tuple(tiers), request_fee_micro=0,
        rounding="half_up_per_million", output_convention="includes_reasoning",
        prompt_convention="excludes_cache" if endpoint.provider == "anthropic" else "includes_cache")



def project(endpoints: tuple[ModelEndpoint, ...], created_at: str,
            document: dict[str, Any] | None = None) -> b.BillingSnapshot:
    if not endpoints or len(endpoints) > 128:
        raise ValueError("snapshot_size")
    candidates = []
    for endpoint in endpoints:
        frozen = candidate(effective_endpoint(endpoint, at=created_at))
        if document is not None:
            source = next(c for c in document["candidates"] if c["endpoint_id"] == endpoint.id)
            frozen = b.Candidate.model_validate({**frozen.model_dump(), **source})
        candidates.append(frozen)
    ordered = tuple(sorted(candidates, key=lambda c: c.endpoint_id))
    if len({c.endpoint_id for c in ordered}) != len(ordered):
        raise ValueError("duplicate candidates")
    # Each candidate was validated once in this immutable view's projection.
    # Revalidating every nested string twice here costs milliseconds at 43
    # candidates. All snapshot-level fields are constants; ordering/uniqueness
    # and local bounds are checked above, never trusted from the wire.
    return b.BillingSnapshot.model_construct(v=1, kind="credits_endpoint", candidates=ordered,
        minimum_charge="one_micro_if_positive", charge_cap=None, tier_basis="total_prompt",
        tier_boundary="inclusive", tier_fallback="last_tier")
