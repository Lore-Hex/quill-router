from __future__ import annotations

import json
from pathlib import Path

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog import (
    MODELS,
    Model,
    ModelEndpoint,
    endpoints_for_model,
)
from trusted_router.pricing import _customer_price, select_price_tier
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars


@pytest.mark.parametrize("prompt_tokens", [199_999, 200_000, 200_001])
@pytest.mark.parametrize("cached_tokens", [0, 100_000])
def test_grok_47_billing_matches_xai_at_long_context_boundary(
    prompt_tokens: int, cached_tokens: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # xAI's Grok 4.7 rate card as its manifest row states it: a prompt of 200K
    # tokens or more pays double on every rate. The catalog's own ingest builds
    # the route from that row, so this holds whatever xAI lists today.
    low = {
        "input_token_price_per_m": 2_000_000,
        "cached_input_token_price_per_m": 500_000,
        "output_token_price_per_m": 6_000_000,
    }
    high = {field: 2 * rate for field, rate in low.items()}
    (tmp_path / "grok.json").write_text(json.dumps({
        "provider": "grok", "price_scale": "microdollars_per_million",
        "models": [{
            "id": "x-ai/grok-4.7", "upstream_id": "grok-4.7", "model_type": "chat",
            "endpoints": ["chat/completions"], **low,
            "price_tiers": [
                {"max_prompt_tokens": 199_999, **low},
                {"max_prompt_tokens": None, **high},
            ],
        }],
    }))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    model = models["x-ai/grok-4.7"]
    endpoint = endpoints["x-ai/grok-4.7@grok/prepaid"]
    multiplier = 1 if prompt_tokens < 200_000 else 2
    input_rate = _customer_price(2_000_000 * multiplier)
    cached_rate = _customer_price(500_000 * multiplier)
    output_rate = _customer_price(6_000_000 * multiplier)
    tier = select_price_tier(endpoint.price_tiers, prompt_tokens)
    assert tier.prompt_price_microdollars_per_million_tokens == input_rate
    assert tier.prompt_cached_price_microdollars_per_million_tokens == cached_rate
    assert tier.completion_price_microdollars_per_million_tokens == output_rate
    expected = sum(
        (tokens * rate + 500_000) // 1_000_000
        for tokens, rate in (
            (prompt_tokens - cached_tokens, input_rate),
            (cached_tokens, cached_rate),
            (100, output_rate),
        )
    )
    assert cost_microdollars(
        model, prompt_tokens, 100, cached_input_tokens=cached_tokens,
    ) == expected
    assert _endpoint_cost_microdollars(
        # Stage D takes uncached input separately; the model helper takes total input.
        endpoint, prompt_tokens - cached_tokens, 100, cache_read_tokens=cached_tokens,
    ) == expected


def _aligned_credit_endpoints() -> list[tuple[Model, ModelEndpoint]]:
    aligned: list[tuple[Model, ModelEndpoint]] = []
    for model in MODELS.values():
        if not model.supports_chat:
            continue
        for endpoint in endpoints_for_model(model.id):
            if endpoint.usage_type != "Credits":
                continue
            if (
                endpoint.prompt_price_microdollars_per_million_tokens
                != model.prompt_price_microdollars_per_million_tokens
            ):
                continue
            if (
                endpoint.completion_price_microdollars_per_million_tokens
                != model.completion_price_microdollars_per_million_tokens
            ):
                continue
            if endpoint.price_tiers != model.price_tiers:
                continue
            aligned.append((model, endpoint))
    return aligned


def test_aligned_credit_endpoint_costs_match_model_helper_no_cache() -> None:
    aligned = _aligned_credit_endpoints()
    multi_tier_model_ids = {
        model.id for model, _endpoint in aligned if len(model.price_tiers) > 1
    }

    assert aligned
    assert multi_tier_model_ids

    for model, endpoint in aligned:
        for prompt_tokens in (1_000, 100_000, 300_000):
            assert _endpoint_cost_microdollars(
                endpoint,
                prompt_tokens,
                2_000,
            ) == cost_microdollars(
                model,
                prompt_tokens,
                2_000,
            )
