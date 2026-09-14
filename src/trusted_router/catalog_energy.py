"""Provider-declared energy eligibility, separate from hardware attestation."""

from trusted_router.catalog_data import PROVIDERS, Provider

GREEN_MODEL_ID = "trustedrouter/green"
GREEN_ENERGY_SOURCE = "https://regolo.ai/sustainable-ai/"


def provider_has_renewable_inference(provider: Provider) -> bool:
    return bool(provider.renewable_energy_inference and provider.energy_policy_url)


def renewable_provider_slugs() -> frozenset[str]:
    return frozenset(
        slug for slug, provider in PROVIDERS.items()
        if slug != "trustedrouter" and provider_has_renewable_inference(provider)
    )
