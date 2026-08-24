from __future__ import annotations

from trusted_router.catalog import MODELS, Model, ModelEndpoint, endpoints_for_model
from trusted_router.routes.helpers import cost_microdollars
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars


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
