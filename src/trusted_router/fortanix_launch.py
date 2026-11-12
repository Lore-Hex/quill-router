"""Exact operator-confirmed Fortanix introductory tariff, not a scraped quote."""

from dataclasses import dataclass
from decimal import Decimal

from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.pricing import _customer_price, _flat_tier

MODEL_ID = "fortanix/glm-5.3-flash"
NATIVE_ID = "GLM-5.3-Flash"
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


# The same model served by every other provider; the offer page compares
# against its live Credits routes.
COMPARISON_MODEL_ID = "z-ai/glm-5.3-flash"


@dataclass(frozen=True)
class LaunchLedePrices:
    """Live per-1M prices for the offer lede on the Fortanix model page.

    The others are the lowest input and lowest output price among the
    comparison model's Credits routes, or None when it has none.
    """

    input_microdollars: int
    output_microdollars: int
    others_input_microdollars: int | None
    others_output_microdollars: int | None

    @property
    def cheapest(self) -> bool:
        """The "lowest-price way" claim holds only against real alternatives."""
        return (
            self.others_input_microdollars is not None
            and self.others_output_microdollars is not None
            and self.input_microdollars < self.others_input_microdollars
            and self.output_microdollars < self.others_output_microdollars
        )


def launch_lede_prices() -> LaunchLedePrices | None:
    """None whenever the offer banner would be hidden."""
    from trusted_router.catalog import MODELS, endpoints_for_model

    if not fortanix_launch_offer():
        return None
    route = endpoints_for_model(MODEL_ID)[0]
    comparison = MODELS.get(COMPARISON_MODEL_ID)
    others = (
        [
            endpoint
            for endpoint in endpoints_for_model(comparison.id)
            if endpoint.usage_type == "Credits"
        ]
        if comparison is not None and not comparison.internal_only
        else []
    )
    return LaunchLedePrices(
        input_microdollars=route.prompt_price_microdollars_per_million_tokens,
        output_microdollars=route.completion_price_microdollars_per_million_tokens,
        others_input_microdollars=min(
            (item.prompt_price_microdollars_per_million_tokens for item in others), default=None
        ),
        others_output_microdollars=min(
            (item.completion_price_microdollars_per_million_tokens for item in others),
            default=None,
        ),
    )


GLM_FLASH_MODEL_ID = COMPARISON_MODEL_ID
GLM_FLASH_ID_SUFFIX = "/glm-5.3-flash"


@dataclass(frozen=True)
class LaunchOfferFacts:
    """Live facts that search and agent-facing copy may state about the offer."""

    model_id: str
    path: str
    price_label: str
    price_phrase: str
    lowest_glm_flash_price: bool
    # One per-1M price when input and output bill the same, else None.
    unit_price: str | None = None


def _cents_label(microdollars_per_million: int) -> str:
    if microdollars_per_million % 10_000 == 0 and microdollars_per_million < 1_000_000:
        return f"{microdollars_per_million // 10_000}¢"
    value = Decimal(microdollars_per_million) / Decimal(1_000_000)
    return f"${value.normalize():f}"


def launch_offer_facts() -> LaunchOfferFacts | None:
    """Return live offer copy facts, or None whenever the banner gate is off.

    Prices come from the public route, so a repriced or removed offer stops
    every claim at once instead of leaving a stale number in search snippets.
    """
    if not fortanix_launch_offer():
        return None
    from trusted_router.catalog import MODELS, endpoints_for_model

    route = endpoints_for_model(MODEL_ID)[0]
    prompt = route.prompt_price_microdollars_per_million_tokens
    completion = route.completion_price_microdollars_per_million_tokens
    if prompt == completion:
        price_label = f"{_cents_label(prompt)} per million tokens"
        price_phrase = f"{price_label} in and out"
    else:
        price_label = price_phrase = (
            f"{_cents_label(prompt)} input and {_cents_label(completion)} output "
            "per million tokens"
        )
    other_routes = [
        endpoint
        for model in MODELS.values()
        if model.id != MODEL_ID
        and not model.internal_only
        and model.id.casefold().endswith(GLM_FLASH_ID_SUFFIX)
        for endpoint in endpoints_for_model(model.id)
        if endpoint.usage_type == "Credits"
    ]
    lowest = bool(other_routes) and all(
        prompt < endpoint.prompt_price_microdollars_per_million_tokens
        and completion < endpoint.completion_price_microdollars_per_million_tokens
        for endpoint in other_routes
    )
    return LaunchOfferFacts(
        model_id=MODEL_ID,
        path=f"/models/{MODEL_ID}",
        price_label=price_label,
        price_phrase=price_phrase,
        lowest_glm_flash_price=lowest,
        unit_price=_cents_label(prompt) if prompt == completion else None,
    )
