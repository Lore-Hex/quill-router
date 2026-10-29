from dataclasses import replace

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION
from trusted_router.catalog_deployment import apply_gateway_admission_holds
from trusted_router.catalog_privacy import (
    endpoint_meets_privacy_requirement,
    endpoint_retention_metadata,
)
from trusted_router.provider_contracts import (
    BEDROCK_RUNTIME_PROFILES,
    provider_model_available_from_gateway_region,
)
from trusted_router.routing_snapshot import _gateway_regions


@pytest.mark.parametrize("model,profile", BEDROCK_RUNTIME_PROFILES.items())
@pytest.mark.parametrize("variant", ["approved", "mantle", "global", "byok", "different-model", "missing"])
def test_only_exact_managed_runtime_profiles_can_be_zdr(model, profile, variant):
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="amazon-bedrock",
                       model_id=model, upstream_id=profile, usage_type="Credits")
    if variant == "mantle":
        endpoint = replace(endpoint, upstream_id=profile.removeprefix("us."))
    elif variant == "global":
        endpoint = replace(endpoint, upstream_id=profile.replace("us.", "global.", 1))
    elif variant == "byok":
        endpoint = replace(endpoint, usage_type="BYOK")
    elif variant == "different-model":
        endpoint = replace(endpoint, model_id="anthropic/claude-new")
    elif variant == "missing":
        endpoint = replace(endpoint, upstream_id=None)
    metadata = endpoint_retention_metadata(endpoint)
    assert metadata["strict_zdr_eligible"] is (variant == "approved")
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION) is (variant == "approved")
    assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)


@pytest.mark.parametrize("region", ["us-central1", "us-east4", "europe-west4", "us-west1", "aws-eu-west-1", "azure-uaenorth", ""])
def test_unqualified_gateway_never_gets_runtime_route(monkeypatch, region):
    from trusted_router import provider_contracts, routing_snapshot

    monkeypatch.setattr(provider_contracts, "BEDROCK_RUNTIME_GATEWAY_REGIONS", frozenset())
    monkeypatch.setattr(routing_snapshot, "BEDROCK_RUNTIME_GATEWAY_REGIONS", frozenset())
    model = "anthropic/claude-haiku-5.5"
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="amazon-bedrock",
                       model_id=model, upstream_id=BEDROCK_RUNTIME_PROFILES[model], usage_type="Credits")
    assert not provider_model_available_from_gateway_region("amazon-bedrock", model, region)
    snapshot = _gateway_regions(endpoint)
    assert snapshot.only == () and snapshot.requires_explicit_origin
    monkeypatch.setenv("TR_SERVING_REGION", region)
    endpoints = {endpoint.id: endpoint}
    apply_gateway_admission_holds({}, endpoints)
    assert not endpoints


@pytest.mark.parametrize("region", ["aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast"])
def test_qualified_runtime_does_not_expand_legacy_mantle_regions(monkeypatch, region):
    from trusted_router import provider_contracts, routing_snapshot

    qualified = frozenset({region})
    monkeypatch.setattr(provider_contracts, "BEDROCK_RUNTIME_GATEWAY_REGIONS", qualified)
    monkeypatch.setattr(routing_snapshot, "BEDROCK_RUNTIME_GATEWAY_REGIONS", qualified)
    model = "anthropic/claude-haiku-5.5"
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="amazon-bedrock",
                       model_id=model, upstream_id=BEDROCK_RUNTIME_PROFILES[model], usage_type="Credits")
    assert provider_model_available_from_gateway_region("amazon-bedrock", model, region)
    assert not provider_model_available_from_gateway_region("amazon-bedrock", "qwen/qwen3-32b", region)
    snapshot = _gateway_regions(endpoint)
    assert snapshot.only == (region,) and snapshot.requires_explicit_origin
    monkeypatch.setenv("TR_SERVING_REGION", region)
    endpoints = {endpoint.id: endpoint}
    apply_gateway_admission_holds({}, endpoints)
    assert endpoints == {endpoint.id: endpoint}
