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


# The base model page for the same weights; its FAQ points at the offer.
COMPARISON_MODEL_ID = "z-ai/glm-5.3-flash"
# Every public model whose ID ends like this serves the same weights, so the
# lowest-price claim and the "other routes" comparison cover all of them.
GLM_FLASH_ID_SUFFIX = "/glm-5.3-flash"


def cents_label(microdollars_per_million: int) -> str:
    """A whole-cent per-1M price under a dollar as "1¢"; anything else in dollars."""
    cents, remainder = divmod(microdollars_per_million, 10_000)
    if remainder == 0 and 0 < cents < 100:
        return f"{cents}\u00a2"
    value = Decimal(microdollars_per_million) / Decimal(1_000_000)
    return f"${value.normalize():f}"


@dataclass(frozen=True)
class LaunchOfferFacts:
    """Live facts every offer surface may state: page hero, search text, llms files.

    Built only while the offer gate is on, from the live route and catalog, so a
    repriced or removed offer stops every claim at once.
    """

    model_id: str
    model_name: str
    provider_name: str
    input_microdollars: int
    output_microdollars: int
    # Lowest prices among the other public GLM 5.3 Flash routes, None when none exist.
    others_input_microdollars: int | None
    others_output_microdollars: int | None

    @property
    def path(self) -> str:
        return f"/models/{self.model_id}"

    @property
    def unit_price(self) -> str | None:
        """One per-1M price when input and output bill the same, else None."""
        if self.input_microdollars != self.output_microdollars:
            return None
        return cents_label(self.input_microdollars)

    @property
    def lowest(self) -> bool:
        """Strictly cheaper than every other GLM 5.3 Flash route, both directions."""
        return (
            self.others_input_microdollars is not None
            and self.others_output_microdollars is not None
            and self.input_microdollars < self.others_input_microdollars
            and self.output_microdollars < self.others_output_microdollars
        )

    @property
    def price_label(self) -> str:
        if self.unit_price is not None:
            return f"{self.unit_price} per million tokens"
        return (
            f"{cents_label(self.input_microdollars)} input and "
            f"{cents_label(self.output_microdollars)} output per million tokens"
        )

    @property
    def price_phrase(self) -> str:
        suffix = " in and out" if self.unit_price is not None else ""
        return f"{self.price_label}{suffix}"


def launch_offer_facts() -> LaunchOfferFacts | None:
    """Return live offer facts, or None whenever the banner gate is off."""
    if not fortanix_launch_offer():
        return None
    from trusted_router.catalog import MODELS, PROVIDERS, endpoints_for_model

    model = MODELS[MODEL_ID]
    route = endpoints_for_model(MODEL_ID)[0]
    provider = PROVIDERS.get(model.provider)
    provider_name = provider.name if provider is not None else model.provider
    others = [
        endpoint
        for other in MODELS.values()
        if other.id != MODEL_ID
        and not other.internal_only
        and other.id.casefold().endswith(GLM_FLASH_ID_SUFFIX)
        for endpoint in endpoints_for_model(other.id)
        if endpoint.usage_type == "Credits"
    ]
    return LaunchOfferFacts(
        model_id=MODEL_ID,
        model_name=model.name.removesuffix(f" ({provider_name})"),
        provider_name=provider_name,
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
