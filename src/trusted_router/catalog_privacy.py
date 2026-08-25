"""Privacy-posture logic for catalog providers and endpoints.

The PRIVACY_TIER_* integer values are stable API vocabulary. They are not an
implication ladder: confidential compute describes where plaintext is
processed, while no-store and ZDR describe retention. Routing decisions use
``endpoint_meets_privacy_requirement`` so those dimensions cannot be
conflated. The TR gateway hop is always attested; these values describe the
upstream provider. Split out of the catalog.py god-module (#38); this module
depends only on the catalog_data leaf and therefore cannot create a cycle.
"""

from __future__ import annotations

from trusted_router.catalog_data import (
    _MODEL_PROVIDER_PRIVACY_OVERRIDES,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    ModelEndpoint,
    ModelProviderPrivacyOverride,
    Provider,
)
from trusted_router.wafer_policy import wafer_zdr_support


def provider_privacy_tier(provider: Provider) -> int:
    """Return the provider's primary display posture.

    Enforcement must use ``endpoint_meets_privacy_requirement`` rather than
    comparing this value numerically.
    """
    if provider.provider_confidential_compute and provider.provider_e2ee:
        return PRIVACY_TIER_CONFIDENTIAL
    if provider.provider_zero_data_retention:
        return PRIVACY_TIER_ZERO_RETENTION
    if provider.stores_content is False:
        return PRIVACY_TIER_NO_STORE
    return PRIVACY_TIER_STANDARD


def _model_provider_privacy_override(
    model_id: str, provider_slug: str
) -> ModelProviderPrivacyOverride | None:
    override = _MODEL_PROVIDER_PRIVACY_OVERRIDES.get(
        (model_id, provider_slug)
    ) or _MODEL_PROVIDER_PRIVACY_OVERRIDES.get((model_id, "*"))
    if override is not None or provider_slug != "wafer":
        return override

    zdr_supported = wafer_zdr_support(model_id)
    if zdr_supported is None:
        return None
    return ModelProviderPrivacyOverride(
        privacy_tier=(PRIVACY_TIER_ZERO_RETENTION if zdr_supported else PRIVACY_TIER_STANDARD),
        provider_zero_data_retention=zdr_supported,
        provider_policy=(
            "Wafer's authenticated model catalog reports that this exact route "
            "supports request-scoped ZDR via Wafer-ZDR: required."
            if zdr_supported
            else "Wafer's authenticated model catalog does not report ZDR support "
            "for this exact route."
        ),
    )


def model_provider_privacy_tier(model_id: str, provider_slug: str) -> int:
    override = _model_provider_privacy_override(model_id, provider_slug)
    if override is not None:
        return override.privacy_tier
    return provider_privacy_tier(PROVIDERS[provider_slug])


def endpoint_privacy_tier(endpoint: ModelEndpoint) -> int:
    override = _model_provider_privacy_override(endpoint.model_id, endpoint.provider)
    if override is not None:
        return override.privacy_tier
    provider = PROVIDERS[endpoint.provider]
    if endpoint.usage_type == "Credits" and provider.prepaid_zero_data_retention:
        return max(provider_privacy_tier(provider), PRIVACY_TIER_ZERO_RETENTION)
    return provider_privacy_tier(provider)


def endpoint_stores_content(endpoint: ModelEndpoint) -> bool:
    """Return the retention posture for this exact provider/model route."""
    override = _model_provider_privacy_override(endpoint.model_id, endpoint.provider)
    if override is not None and override.stores_content is not None:
        return override.stores_content
    if endpoint_zero_data_retention(endpoint) is True:
        return False
    if override is not None and override.privacy_tier in {
        PRIVACY_TIER_NO_STORE,
        PRIVACY_TIER_ZERO_RETENTION,
    }:
        return False
    return PROVIDERS[endpoint.provider].stores_content


def endpoint_zero_data_retention(endpoint: ModelEndpoint) -> bool | None:
    """Return the ZDR guarantee that applies to this exact credential path.

    Deliberately reads the provider's own flag rather than deriving from the
    tier. An earlier version of this change derived it — CONFIDENTIAL implies
    ZDR — to close a case where the router admits a route for a `zdr` floor
    while this function reports False. Review rejected that reasoning and was
    right: confidential compute means the provider cannot READ the content, and
    says nothing about whether it RETAINS the ciphertext or has a deletion
    policy. Publishing ZDR on that basis would assert a stronger claim than the
    provider itself makes, which is the exact failure mode this whole tier
    system exists to prevent.

    The contradictory combination (confidential compute + e2ee together with an
    explicit provider_zero_data_retention=False) is instead forbidden at the
    catalog level — see
    tests/test_catalog_privacy_coherence_property.py::
    test_no_shipped_provider_has_the_contradictory_flag_combination. No shipped
    provider has it; the test fails loudly if a catalog edit introduces one,
    rather than either function quietly inventing an answer.
    """
    override = _model_provider_privacy_override(endpoint.model_id, endpoint.provider)
    if override is not None and override.provider_zero_data_retention is not None:
        return override.provider_zero_data_retention
    provider = PROVIDERS[endpoint.provider]
    if endpoint.usage_type == "Credits" and provider.prepaid_zero_data_retention:
        return True
    return provider.provider_zero_data_retention


def endpoint_zero_data_retention_scope(endpoint: ModelEndpoint) -> str | None:
    """Describe why an endpoint qualifies without broadening the claim."""
    if endpoint_zero_data_retention(endpoint) is not True:
        return None
    override = _model_provider_privacy_override(endpoint.model_id, endpoint.provider)
    if override is not None and override.provider_zero_data_retention is True:
        return "model_endpoint"
    provider = PROVIDERS[endpoint.provider]
    if endpoint.usage_type == "Credits" and provider.prepaid_zero_data_retention:
        return "trustedrouter_prepaid"
    return "provider"


def model_provider_confidential_compute(model_id: str, provider_slug: str) -> bool | None:
    override = _model_provider_privacy_override(model_id, provider_slug)
    if override is not None and override.provider_confidential_compute is not None:
        return override.provider_confidential_compute
    return PROVIDERS[provider_slug].provider_confidential_compute


def model_provider_e2ee(model_id: str, provider_slug: str) -> bool | None:
    override = _model_provider_privacy_override(model_id, provider_slug)
    if override is not None and override.provider_e2ee is not None:
        return override.provider_e2ee
    return PROVIDERS[provider_slug].provider_e2ee


def endpoint_confidential_compute(endpoint: ModelEndpoint) -> bool | None:
    return model_provider_confidential_compute(endpoint.model_id, endpoint.provider)


def endpoint_e2ee(endpoint: ModelEndpoint) -> bool | None:
    return model_provider_e2ee(endpoint.model_id, endpoint.provider)


def endpoint_meets_privacy_requirement(endpoint: ModelEndpoint, requirement: int) -> bool:
    """Match one requested privacy guarantee without conflating dimensions.

    The integer values are stable API vocabulary, not a logical implication
    chain. In particular, confidential compute proves where plaintext can be
    processed; it does not by itself promise deletion or zero retention.
    """
    if requirement == PRIVACY_TIER_STANDARD:
        return True
    if requirement == PRIVACY_TIER_NO_STORE:
        return not endpoint_stores_content(endpoint)
    if requirement == PRIVACY_TIER_ZERO_RETENTION:
        return endpoint_zero_data_retention(endpoint) is True
    if requirement == PRIVACY_TIER_CONFIDENTIAL:
        return endpoint_confidential_compute(endpoint) is True and endpoint_e2ee(endpoint) is True
    return False


def model_provider_zero_data_retention(model_id: str, provider_slug: str) -> bool | None:
    override = _model_provider_privacy_override(model_id, provider_slug)
    if override is not None and override.provider_zero_data_retention is not None:
        return override.provider_zero_data_retention
    return PROVIDERS[provider_slug].provider_zero_data_retention


def model_provider_policy(model_id: str, provider_slug: str) -> str:
    override = _model_provider_privacy_override(model_id, provider_slug)
    if override is not None and override.provider_policy is not None:
        return override.provider_policy
    return PROVIDERS[provider_slug].provider_policy


def model_provider_policy_url(model_id: str, provider_slug: str) -> str | None:
    override = _model_provider_privacy_override(model_id, provider_slug)
    if override is not None and override.provider_policy_url is not None:
        return override.provider_policy_url
    return PROVIDERS[provider_slug].provider_policy_url
