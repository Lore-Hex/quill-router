"""Deterministic facts used by provider Open Graph cards."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from trusted_router.catalog import META_MODEL_IDS, MODEL_ENDPOINTS, PROVIDERS


@dataclass(frozen=True)
class ProviderOgFacts:
    slug: str
    name: str
    model_count: int
    route_count: int
    route_mode: str
    privacy: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def provider_og_facts(provider_slug: str) -> ProviderOgFacts:
    """Return the catalog facts printed on one provider's social card."""
    provider = PROVIDERS[provider_slug]
    endpoints = tuple(
        endpoint
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == provider_slug and endpoint.model_id not in META_MODEL_IDS
    )
    model_count = len({endpoint.model_id for endpoint in endpoints})
    has_byok = any(endpoint.is_byok for endpoint in endpoints)
    has_prepaid = any(not endpoint.is_byok for endpoint in endpoints)
    if has_prepaid and has_byok:
        route_mode = "Prepaid + BYOK"
    elif has_prepaid:
        route_mode = "Prepaid"
    elif has_byok:
        route_mode = "BYOK"
    else:
        route_mode = "Catalog profile"

    if provider.provider_e2ee and provider.provider_confidential_compute:
        privacy = "E2EE + confidential"
    elif provider.provider_zero_data_retention or provider.prepaid_zero_data_retention:
        privacy = "Zero retention"
    elif provider.provider_confidential_compute:
        privacy = "Confidential compute"
    else:
        privacy = "Policy documented"

    return ProviderOgFacts(
        slug=provider.slug,
        name=provider.name,
        model_count=model_count,
        route_count=len(endpoints),
        route_mode=route_mode,
        privacy=privacy,
    )


def all_provider_og_facts() -> tuple[ProviderOgFacts, ...]:
    return tuple(provider_og_facts(slug) for slug in sorted(PROVIDERS))
