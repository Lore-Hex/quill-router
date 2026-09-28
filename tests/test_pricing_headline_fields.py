"""A provider-priced headline carries only the rates that provider sets.

`_merge_snapshot` used to start each model's headline from OpenRouter's
model-level aggregate and overlay the cheapest TR provider's block. Rates the
provider does not set survived from OpenRouter, even when they came from a
provider TR does not route to. On 2026-09-28 the committed snapshot showed
this: `meta-llama/llama-3.1-8b-instruct` had headline `input_cache_read`
0.000000025 while its routes charged 0.0000002 or nothing. Headlines also
carried `web_search` in 61 models, `input_cache_write` in 30 and `overrides`
(time-of-day prices) in 21, none of which any TR provider sets.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.pricing import refresh as R
from scripts.pricing.base import ModelPrice, PriceTier

MODEL_ID = "meta-llama/llama-3.1-8b-instruct"

# OpenRouter's model-level aggregate, as its feed publishes it.
OPENROUTER_AGGREGATE = {
    "prompt": "0.00000002",
    "completion": "0.00000005",
    "input_cache_read": "0.000000025",
    "input_cache_write": "0.00000003",
    "web_search": "0.01",
    "overrides": [
        {"utc_start": 0, "utc_end": 1600, "prompt": "0.00000002", "completion": "0.00000005"}
    ],
}

INHERITED_ONLY = {"input_cache_write", "web_search", "overrides"}


def _or_snapshot(slugs: list[str]) -> dict[str, Any]:
    return {
        "models": [
            {
                "id": MODEL_ID,
                "name": MODEL_ID,
                "context_length": 131072,
                "pricing": dict(OPENROUTER_AGGREGATE),
                "endpoints": [
                    {"model_id": MODEL_ID, "tr_provider_slug": slug, "context_length": 131072}
                    for slug in slugs
                ],
            }
        ],
        "tr_keyed_providers": slugs,
    }


def _headline(provider_index: dict[str, ModelPrice]) -> dict[str, Any]:
    merged = R._merge_snapshot(_or_snapshot(sorted(provider_index)), {MODEL_ID: provider_index}, set())
    (model,) = merged["models"]
    assert model["pricing_source"] == "provider_direct"
    return dict(model["pricing"])


@pytest.mark.parametrize(
    ("cached_micro_per_m", "expected_cache_read"),
    [(None, None), (20_000, "0.00000002")],
    ids=["provider-sets-no-cache-read", "provider-sets-cache-read"],
)
def test_headline_cache_read_is_the_providers_own_or_absent(
    cached_micro_per_m: int | None, expected_cache_read: str | None
) -> None:
    price = ModelPrice(30_000, 50_000, prompt_cached_micro_per_m=cached_micro_per_m)

    headline = _headline({"deepinfra": price})

    assert headline.get("input_cache_read") == expected_cache_read
    assert headline == R._price_to_pricing_block(price)
    assert not INHERITED_ONLY & headline.keys()


def test_headline_is_the_cheapest_providers_block_not_a_mix() -> None:
    # The dearer provider prices cache reads; the cheapest does not.
    cheapest = ModelPrice(20_000, 50_000)
    dearer = ModelPrice(30_000, 60_000, prompt_cached_micro_per_m=200_000)

    merged = R._merge_snapshot(
        _or_snapshot(["deepinfra", "novita"]),
        {MODEL_ID: {"deepinfra": cheapest, "novita": dearer}},
        set(),
    )

    (model,) = merged["models"]
    assert model["pricing"] == {"prompt": "0.00000002", "completion": "0.00000005"}
    novita = next(ep for ep in model["endpoints"] if ep["tr_provider_slug"] == "novita")
    assert novita["pricing"]["input_cache_read"] == "0.0000002"


def test_headline_keeps_the_providers_own_tiers() -> None:
    price = ModelPrice(
        tiers=[
            PriceTier(
                max_prompt_tokens=200_000,
                prompt_micro_per_m=30_000,
                completion_micro_per_m=50_000,
                prompt_cached_micro_per_m=3_000,
            ),
            PriceTier(
                max_prompt_tokens=None,
                prompt_micro_per_m=60_000,
                completion_micro_per_m=100_000,
            ),
        ]
    )

    headline = _headline({"deepinfra": price})

    assert headline == R._price_to_pricing_block(price)
    assert [tier["prompt"] for tier in headline["prompt_tiers"]] == ["0.00000003", "0.00000006"]
    assert headline["input_cache_read"] == "0.000000003"


def test_openrouter_fallback_headline_is_still_openrouters_aggregate() -> None:
    # When every keyed provider prices $0, OpenRouter is the deliberate price
    # source, so its aggregate rates stay with its headline.
    merged = R._merge_snapshot(
        _or_snapshot(["deepinfra"]), {MODEL_ID: {"deepinfra": ModelPrice(0, 0)}}, set()
    )

    (model,) = merged["models"]
    assert model["pricing_source"] == "openrouter_fallback"
    assert model["pricing"] == OPENROUTER_AGGREGATE
