"""Eligibility of concrete model/operator pairs under a workspace origin policy."""

from trusted_router.catalog import MODELS, PROVIDERS
from trusted_router.catalog_aliases import canonical_listing_id
from trusted_router.catalog_data import Model, ModelEndpoint, model_origin_for_model_id
from trusted_router.managed_retention import REVIEWED_ROUTES
from trusted_router.model_regions import EU_MEMBER_COUNTRIES

COUNTRIES = {
    "us": frozenset({"US"}),
    "western": EU_MEMBER_COUNTRIES | {"US", "CA", "IS", "LI", "NO", "GB", "CH", "AU", "NZ"},
}
POLICIES = frozenset({"unrestricted", *COUNTRIES})

# A broker's headquarters alone cannot identify the company serving the call.
# Admit only catalog operators whose native route has a known operator.
DIRECT_OPERATORS = frozenset({
    "amazon-bedrock", "anthropic", "azure", "cerebras", "cohere", "deepinfra",
    "fireworks", "google-ai-studio", "google-vertex", "mistral", "openai",
    "privatemode", "sambanova", "together",
})


def eligible_candidates(
    policy: str, candidates: list[tuple[Model, ModelEndpoint]],
) -> list[tuple[Model, ModelEndpoint]]:
    if policy == "unrestricted":
        return candidates
    countries = COUNTRIES.get(policy) if isinstance(policy, str) else None
    if not countries:
        return []
    allowed = []
    for model, endpoint in candidates:
        model_id = canonical_listing_id(model.id, MODELS)
        if model_id.partition("/")[0] in {"trustedrouter", "nyte"}:
            continue  # A router's jurisdiction cannot establish its constituents' origins.
        if model_id not in MODELS or model_id != canonical_listing_id(endpoint.model_id, MODELS):
            continue
        if endpoint.provider == "azure" and (
            endpoint.is_byok or (endpoint.provider, model_id, endpoint.upstream_id or "") not in REVIEWED_ROUTES
        ):
            continue  # Only these managed deployments are confirmed Azure-hosted, not partner-hosted.
        if endpoint.provider == "google-vertex" and not model_id.startswith("google/"):
            continue  # A partner listing alone does not identify the operator.
        origin = model_origin_for_model_id(model_id)
        operator = PROVIDERS.get(endpoint.provider)
        if (
            origin and origin.country in countries and endpoint.provider in DIRECT_OPERATORS
            and operator and operator.provider_headquarters_country in countries
        ):
            allowed.append((model, endpoint))
    return allowed
