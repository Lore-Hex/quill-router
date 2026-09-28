"""A provider-priced endpoint carries only the rates that provider sets.

`_merge_snapshot` used to start each endpoint's pricing from OpenRouter's
listing of that endpoint and overlay the provider's own block, so a rate the
provider's price does not state survived from OpenRouter. On 2026-09-28 the
committed snapshot had 10 such cached-input rates: Mistral's codestral-2508,
mistral-large and mistral-small-2603 under eight OpenRouter tags, Cerebras'
gpt-oss-120b (fp16) and DeepInfra's gemma-4-31b-it (turbo). None was billed
that day, because a manifest endpoint replaced each route. A provider-priced
route with no manifest endpoint bills straight from this block, though, and
the stale fallback carries a committed block's rates into the next snapshot
when the provider's refresh fails. #1385 fixed the same leak in the headline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import refresh as R
from scripts.pricing.base import ModelPrice, PriceTier
from trusted_router import catalog_ingest
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars

MODEL_ID = "mistralai/mistral-large"
SLUG = "mistral"

# OpenRouter's listing of Mistral's mistral-large endpoint, as its feed
# published it: a $0.20/M cached-input rate Mistral's own price does not state.
OPENROUTER_ENDPOINT_PRICING = {
    "prompt": "0.0000005",
    "completion": "0.0000015",
    "input_cache_read": "0.0000002",
    "input_cache_write": "0.0000005",
    "web_search": "0.01",
    "discount": 0,
}
MISTRAL_PRICE = ModelPrice(500_000, 1_500_000)


def _or_snapshot() -> dict[str, Any]:
    return {
        "models": [
            {
                "id": MODEL_ID,
                "name": MODEL_ID,
                "context_length": 256000,
                "pricing": dict(OPENROUTER_ENDPOINT_PRICING),
                "endpoints": [
                    {
                        "name": f"Mistral | {MODEL_ID}",
                        "model_id": MODEL_ID,
                        "tr_provider_slug": SLUG,
                        "context_length": 256000,
                        "pricing": dict(OPENROUTER_ENDPOINT_PRICING),
                    }
                ],
            }
        ],
        "tr_keyed_providers": [SLUG],
    }


def _merged_endpoint(price: ModelPrice) -> tuple[dict[str, Any], dict[str, Any]]:
    merged = R._merge_snapshot(_or_snapshot(), {MODEL_ID: {SLUG: price}}, set())
    (model,) = merged["models"]
    (endpoint,) = model["endpoints"]
    return merged, endpoint


def _billed_per_million_cached_tokens(
    snapshot: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> int:
    """What the gateway bills for one million cached input tokens on the
    route ingest builds from this snapshot, in microdollars."""
    path = tmp_path / "openrouter_snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", path)
    _models, endpoints = catalog_ingest._ingested_models_and_endpoints()
    route = endpoints[f"{MODEL_ID}@{SLUG}/prepaid"]
    return _endpoint_cost_microdollars(route, 0, 0, cache_read_tokens=1_000_000)


def test_cached_input_bills_the_providers_rate_not_openrouters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    after, endpoint = _merged_endpoint(MISTRAL_PRICE)

    # Before: the old merge laid the provider's block over OpenRouter's.
    before = json.loads(json.dumps(after))
    before["models"][0]["endpoints"][0]["pricing"] = {
        **OPENROUTER_ENDPOINT_PRICING,
        **R._price_to_pricing_block(MISTRAL_PRICE),
    }

    # OpenRouter's $0.20/M plus TrustedRouter's 5.5%.
    assert _billed_per_million_cached_tokens(before, tmp_path, monkeypatch) == 211_000
    # Mistral states no cached rate, so its multiplier applies: 0.1 x $0.5275/M.
    assert _billed_per_million_cached_tokens(after, tmp_path, monkeypatch) == 52_750
    assert endpoint["pricing"] == R._price_to_pricing_block(MISTRAL_PRICE)


def test_a_providers_own_cached_input_rate_is_the_one_billed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive control: a provider that states a cached rate keeps it, over
    # both OpenRouter's rate and the provider multiplier.
    price = ModelPrice(500_000, 1_500_000, prompt_cached_micro_per_m=20_000)
    merged, endpoint = _merged_endpoint(price)

    assert endpoint["pricing"]["input_cache_read"] == "0.00000002"
    assert _billed_per_million_cached_tokens(merged, tmp_path, monkeypatch) == 21_100


def test_a_providers_own_tiers_are_the_endpoints_tiers() -> None:
    price = ModelPrice(
        tiers=[
            PriceTier(
                max_prompt_tokens=200_000,
                prompt_micro_per_m=500_000,
                completion_micro_per_m=1_500_000,
                prompt_cached_micro_per_m=50_000,
            ),
            PriceTier(
                max_prompt_tokens=None,
                prompt_micro_per_m=1_000_000,
                completion_micro_per_m=3_000_000,
            ),
        ]
    )

    _merged, endpoint = _merged_endpoint(price)

    assert endpoint["pricing"] == R._price_to_pricing_block(price)


def test_a_failed_refresh_carries_only_the_providers_rate_forward() -> None:
    # A failed refresh reads the committed endpoint block back as the
    # provider's price and publishes it again.
    merged, _endpoint = _merged_endpoint(MISTRAL_PRICE)

    stale = R._stale_results_from_snapshot(merged, [SLUG])[SLUG].prices[MODEL_ID]

    assert stale.tiers[0].prompt_micro_per_m == 500_000
    assert stale.tiers[0].prompt_cached_micro_per_m is None


def test_openrouter_fallback_endpoints_keep_openrouters_listing() -> None:
    # When every keyed provider prices $0, OpenRouter is the deliberate price
    # source, so its endpoint listing is kept whole.
    merged = R._merge_snapshot(_or_snapshot(), {MODEL_ID: {SLUG: ModelPrice(0, 0)}}, set())

    (model,) = merged["models"]
    (endpoint,) = model["endpoints"]
    assert model["pricing_source"] == "openrouter_fallback"
    assert endpoint["pricing"] == OPENROUTER_ENDPOINT_PRICING


def _held_snapshot(pricing: dict[str, Any]) -> dict[str, Any]:
    snapshot = _or_snapshot()
    snapshot["models"][0]["endpoints"][0]["pricing"] = pricing
    return snapshot


def test_a_hold_compares_prices_not_the_keys_older_snapshots_carried() -> None:
    # Snapshots published before this change carry OpenRouter's other keys on
    # provider-priced endpoints. A held provider's route re-merged from its
    # published prices lacks them; that must not read as a changed route, or
    # the first hold after this change would publish nothing for anyone.
    held = {SLUG: [SLUG]}
    published = _held_snapshot(
        {**R._price_to_pricing_block(MISTRAL_PRICE), "discount": 0, "web_search": "0.01"}
    )
    remerged = _held_snapshot(R._price_to_pricing_block(MISTRAL_PRICE))

    assert R._held_endpoint_pricing(published, held) == R._held_endpoint_pricing(remerged, held)


@pytest.mark.parametrize(
    "changed",
    [
        {"prompt": "0.0000006"},
        {"completion": "0.0000016"},
        {"input_cache_read": "0.00000005"},
        {"prompt_tiers": [{"max_prompt_tokens": None, "prompt": "0.0000005"}]},
    ],
    ids=["prompt", "completion", "cached-input", "tiers"],
)
def test_a_hold_still_sees_every_changed_price(changed: dict[str, Any]) -> None:
    held = {SLUG: [SLUG]}
    published = _held_snapshot(R._price_to_pricing_block(MISTRAL_PRICE))
    remerged = _held_snapshot({**R._price_to_pricing_block(MISTRAL_PRICE), **changed})

    assert R._held_endpoint_pricing(published, held) != R._held_endpoint_pricing(remerged, held)
