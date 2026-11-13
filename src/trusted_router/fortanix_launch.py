"""Exact operator-confirmed Fortanix introductory tariff, not a scraped quote."""

from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.pricing import _customer_price, _flat_tier

MODEL_ID = "fortanix/glm-5.3-flash"
NATIVE_ID = "GLM-5.3-Flash"
# Fortanix /v1/models max_model_len; near-limit inference verified 2026-11-12.
# See docs/providers/fortanix-launch-2026-11-10.md, Context Window.
CONTEXT_LENGTH = 1_048_576
# Joseph confirmed zero supplier charges, no campaign cap and no fixed end
# date on 2026-11-10. Do not turn that into a permanent provider price claim.
SUPPLIER_COST_MICRO_PER_M = 0
RETAIL_MICRO_PER_M = _customer_price(SUPPLIER_COST_MICRO_PER_M)
TIERS = _flat_tier(RETAIL_MICRO_PER_M, RETAIL_MICRO_PER_M)


def fortanix_launch_offer() -> bool:
    # The registry imports this module's tariff while constructing the catalog.
    from trusted_router.catalog import (
        MODELS,
        PROVIDERS,
        endpoint_zero_data_retention,
        endpoints_for_model,
    )
    from trusted_router.provider_contracts import provider_route_reviewed_hold
    from trusted_router.provider_locations import provider_inference_locations

    model = MODELS.get(MODEL_ID)
    provider = PROVIDERS.get("fortanix")
    if (
        model is None
        or provider is None
        or provider.internal_only
        or provider_route_reviewed_hold("fortanix", model.id, model.upstream_id)
    ):
        return False
    routes = endpoints_for_model(model.id)
    return (
        len(routes) == 1
        and launch_tariff_matches(model, routes[0])
        and endpoint_zero_data_retention(routes[0]) is True
        and provider_inference_locations("fortanix").locations
        == ("Santa Clara, California, United States",)
    )


def launch_tariff_matches(model: Model, endpoint: ModelEndpoint) -> bool:
    """Neither the coverage exception nor the banner may cover a changed tariff."""
    return (
        model.id == endpoint.model_id == MODEL_ID
        and model.provider == endpoint.provider == "fortanix"
        and model.upstream_id == endpoint.upstream_id == NATIVE_ID
        and not model.internal_only
        and model.prepaid_available
        and not model.byok_available
        and endpoint.usage_type == "Credits"
        and model.minimum_charge_microdollars == 0
        and all(
            item.prompt_price_microdollars_per_million_tokens == RETAIL_MICRO_PER_M
            and item.completion_price_microdollars_per_million_tokens == RETAIL_MICRO_PER_M
            and item.published_prompt_price_microdollars_per_million_tokens == RETAIL_MICRO_PER_M
            and item.published_completion_price_microdollars_per_million_tokens
            == RETAIL_MICRO_PER_M
            and item.request_price_microdollars == 0
            and item.price_tiers == item.published_price_tiers == TIERS
            for item in (model, endpoint)
        )
    )
