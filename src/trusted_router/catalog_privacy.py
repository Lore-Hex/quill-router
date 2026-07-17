"""Privacy-posture tier logic for catalog providers and endpoints.

The PRIVACY_TIER_* ranks + aliases these read live in catalog_data; these
functions apply them to decide the highest privacy bar a provider or endpoint
clears, which is what enforces a request's minimum-privacy routing preference
(the TR gateway hop is always attested regardless — this rank is about the
UPSTREAM provider's posture). Split out of the catalog.py god-module (#38);
catalog.py re-exports these and routing_candidates.py imports
endpoint_privacy_tier. Depends only on the catalog_data leaf, so importing it
never pulls in catalog.py (no cycle)."""

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


def provider_privacy_tier(provider: Provider) -> int:
    """The highest privacy bar a provider clears. Used to enforce a
    request's minimum-privacy routing preference. Note the TR gateway hop
    is always attested regardless of tier — this rank is about the
    UPSTREAM provider's posture, which is what varies."""
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
    return _MODEL_PROVIDER_PRIVACY_OVERRIDES.get(
        (model_id, provider_slug)
    ) or _MODEL_PROVIDER_PRIVACY_OVERRIDES.get((model_id, "*"))


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


def endpoint_zero_data_retention(endpoint: ModelEndpoint) -> bool | None:
    """Return the ZDR guarantee that applies to this exact credential path."""
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
