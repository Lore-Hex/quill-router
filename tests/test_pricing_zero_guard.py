"""Regression tests for the 2026-06 price-refresh freeze.

A single provider returning $0 for a popular open model (a `:free`/preview
variant, a /v1/models row listed without pricing, or an intermittent
omission) used to win `_merge_snapshot`'s cheapest-tier selection, zero the
model's headline, trip check_price_spike's both-prices-to-zero guard, and
freeze the ENTIRE hourly refresh — the committed snapshot went stale for
~2 weeks while ~6 models (gemma-3-4b-it, llama-3.3-70b, ...) were in fact
served at real prices the whole time.
"""
from __future__ import annotations

from scripts.pricing import refresh as R
from scripts.pricing.base import ModelPrice, PriceTier


def _mp(prompt_micro: int, completion_micro: int) -> ModelPrice:
    return ModelPrice(
        tiers=[
            PriceTier(
                max_prompt_tokens=None,
                prompt_micro_per_m=prompt_micro,
                completion_micro_per_m=completion_micro,
            )
        ]
    )


def _or_snapshot(model_id: str, slugs: list[str], pricing: dict[str, str]) -> dict:
    return {
        "models": [
            {
                "id": model_id,
                "name": model_id,
                "context_length": 131072,
                "pricing": pricing,
                "endpoints": [
                    {
                        "name": f"{s} | {model_id}",
                        "model_id": model_id,
                        "tr_provider_slug": s,
                        "context_length": 131072,
                        "pricing": pricing,
                    }
                    for s in slugs
                ],
            }
        ],
        "tr_keyed_providers": slugs,
    }


def test_zero_priced_provider_does_not_win_cheapest() -> None:
    """deepinfra prices gemma-3-4b-it for real; a second provider returns
    $0 (free-tier artifact). The headline must be deepinfra's real price,
    NOT $0 — this is the exact shape that froze the refresh."""
    mid = "google/gemma-3-4b-it"
    or_snap = _or_snapshot(
        mid, ["deepinfra"], {"prompt": "0.00000005", "completion": "0.0000001"}
    )
    provider_index = {
        mid: {
            "deepinfra": _mp(50_000, 100_000),  # real $0.05/$0.10 per M
            "novita": _mp(0, 0),  # spurious free-tier $0 row
        }
    }
    merged = R._merge_snapshot(or_snap, provider_index, set())
    model = next(m for m in merged["models"] if m["id"] == mid)
    assert model["pricing"]["prompt"] == "0.00000005", model["pricing"]
    assert model["pricing"]["completion"] == "0.0000001", model["pricing"]
    assert model["pricing_source"] == "provider_direct"
    # the $0 endpoint must not appear; deepinfra must.
    slugs = {e["tr_provider_slug"] for e in model["endpoints"]}
    assert slugs == {"deepinfra"}, slugs


def test_all_zero_falls_back_to_openrouter_not_zero() -> None:
    """If EVERY keyed provider returns $0 for a model OR prices > $0 (a
    feed glitch across all of them), fall back to OR's price rather than
    emitting $0 (which freezes the refresh) or dropping a served model."""
    mid = "meta-llama/llama-3.3-70b-instruct"
    or_snap = _or_snapshot(
        mid, ["parasail", "tinfoil"], {"prompt": "0.0000001", "completion": "0.00000032"}
    )
    provider_index = {mid: {"parasail": _mp(0, 0), "tinfoil": _mp(0, 0)}}
    merged = R._merge_snapshot(or_snap, provider_index, set())
    model = next(m for m in merged["models"] if m["id"] == mid)
    assert model["pricing"]["prompt"] == "0.0000001", model["pricing"]
    assert model["pricing_source"] == "openrouter_fallback"
    assert model["endpoints"], "OR-fallback model must keep its endpoints"


def test_positive_price_path_unchanged() -> None:
    """No $0 anywhere → behaves exactly as before (cheapest positive tier)."""
    mid = "google/gemma-3-27b-it"
    or_snap = _or_snapshot(
        mid,
        ["deepinfra", "parasail"],
        {"prompt": "0.00000008", "completion": "0.00000016"},
    )
    provider_index = {
        mid: {"deepinfra": _mp(80_000, 160_000), "parasail": _mp(120_000, 450_000)}
    }
    merged = R._merge_snapshot(or_snap, provider_index, set())
    model = next(m for m in merged["models"] if m["id"] == mid)
    # cheapest prompt tier = deepinfra 80_000
    assert model["pricing"]["prompt"] == "0.00000008", model["pricing"]
    assert model["pricing_source"] == "provider_direct"


def test_is_unpriced_helper() -> None:
    assert R._is_unpriced(_mp(0, 0))
    assert not R._is_unpriced(_mp(1, 0))
    assert not R._is_unpriced(_mp(0, 1))
    assert not R._is_unpriced(_mp(50_000, 100_000))
