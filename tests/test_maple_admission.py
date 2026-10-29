from dataclasses import replace
from datetime import UTC, datetime

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_data import model_vendor_provider_slugs
from trusted_router.catalog_deployment import apply_gateway_admission_holds
from trusted_router.provider_contracts import (
    BEDROCK_GATEWAY_REGIONS,
    BEDROCK_RUNTIME_GATEWAY_REGIONS,
    BEDROCK_RUNTIME_PROFILES,
    MAPLE_GATEWAY_REGIONS,
    MAPLE_MODEL_ID,
    MAPLE_NATIVE_ID,
    provider_model_available_from_gateway_region,
)
from trusted_router.routing_snapshot import _gateway_regions


@pytest.mark.parametrize("provider,model", [
    ("maple", MAPLE_MODEL_ID),
    *(("amazon-bedrock", model) for model in BEDROCK_RUNTIME_PROFILES),
])
@pytest.mark.parametrize("setting,region", [
    ("TR_PRIMARY_REGION", "eu-west-1"),
    ("TR_PRIMARY_REGION", "eu-west-3"),
    ("TR_PRIMARY_REGION", "uaenorth"),
    ("TR_SERVING_REGION", "southamerica-east1"),
])
def test_runtime_catalog_uses_real_control_plane_region_settings(monkeypatch, provider, model, setting, region):
    monkeypatch.delenv("TR_SERVING_REGION", raising=False)
    monkeypatch.delenv("TR_PRIMARY_REGION", raising=False)
    monkeypatch.setenv("TR_ENVIRONMENT", "production")
    monkeypatch.setenv(setting, region)
    endpoint = next(e for e in MODEL_ENDPOINTS.values() if e.provider == provider and e.model_id == model)
    endpoints = {endpoint.id: endpoint}
    apply_gateway_admission_holds({}, endpoints)
    assert endpoints == {endpoint.id: endpoint}
    # These are control-plane locations, not newly qualified gateway origins.
    assert not provider_model_available_from_gateway_region(provider, model, region)


@pytest.mark.parametrize("region", ["eu-west-2", "westus", "future-region", ""])
def test_runtime_catalog_unknown_control_plane_still_fails_closed(monkeypatch, region):
    monkeypatch.delenv("TR_SERVING_REGION", raising=False)
    monkeypatch.setenv("TR_PRIMARY_REGION", region)
    monkeypatch.setenv("TR_ENVIRONMENT", "production")
    endpoints = {e.id: e for e in MODEL_ENDPOINTS.values()
                 if e.provider == "maple" or (e.provider == "amazon-bedrock" and e.model_id in BEDROCK_RUNTIME_PROFILES)}
    assert endpoints
    apply_gateway_admission_holds({}, endpoints)
    assert not endpoints


@pytest.mark.parametrize("region", ["eu-west-1", "eu-west-3", "uaenorth", "southamerica-east1"])
@pytest.mark.parametrize("qualified", [frozenset(), frozenset({"azure-australiaeast"})])
def test_control_plane_alias_does_not_admit_unqualified_fleet(monkeypatch, region, qualified):
    from trusted_router import provider_contracts

    monkeypatch.setattr(provider_contracts, "MAPLE_GATEWAY_REGIONS", qualified)
    monkeypatch.setattr(provider_contracts, "BEDROCK_RUNTIME_GATEWAY_REGIONS", qualified)
    monkeypatch.delenv("TR_SERVING_REGION", raising=False)
    monkeypatch.setenv("TR_PRIMARY_REGION", region)
    monkeypatch.setenv("TR_ENVIRONMENT", "production")
    endpoints = {e.id: e for e in MODEL_ENDPOINTS.values()
                 if e.provider == "maple" or (e.provider == "amazon-bedrock" and e.model_id in BEDROCK_RUNTIME_PROFILES)}
    assert endpoints
    apply_gateway_admission_holds({}, endpoints)
    assert not endpoints


def test_release_admission_names_exact_gateway_fleets_without_expanding_mantle():
    expected = frozenset({
        "us-central1", "us-east4", "europe-west4", "us-west1",
        "aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast",
    })
    assert MAPLE_GATEWAY_REGIONS == BEDROCK_RUNTIME_GATEWAY_REGIONS == expected
    assert BEDROCK_GATEWAY_REGIONS == expected - {
        "aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast",
    }
    for region in expected:
        assert provider_model_available_from_gateway_region("maple", MAPLE_MODEL_ID, region)
        for model in BEDROCK_RUNTIME_PROFILES:
            assert provider_model_available_from_gateway_region("amazon-bedrock", model, region)
    for unknown in ("", "aws-us-east-1", "azure-westus", "future-region"):
        assert not provider_model_available_from_gateway_region("maple", MAPLE_MODEL_ID, unknown)
        for model in BEDROCK_RUNTIME_PROFILES:
            assert not provider_model_available_from_gateway_region("amazon-bedrock", model, unknown)


@pytest.mark.parametrize("environment,admitted", [("local", True), ("test", True), ("production", False), ("prod", False)])
def test_preview_default_never_substitutes_for_production_region_evidence(monkeypatch, environment, admitted):
    monkeypatch.delenv("TR_SERVING_REGION", raising=False)
    monkeypatch.delenv("TR_PRIMARY_REGION", raising=False)
    monkeypatch.setenv("TR_ENVIRONMENT", environment)
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="maple",
                       model_id=MAPLE_MODEL_ID, upstream_id=MAPLE_NATIVE_ID, usage_type="Credits")
    endpoints = {endpoint.id: endpoint}
    apply_gateway_admission_holds({}, endpoints)
    assert bool(endpoints) is admitted


def test_maple_checkpoint_names_its_weights_author_not_host():
    # The pinned i02 workload uses dealignai's immutable uncensored checkpoint.
    # Renaming a stock model into the host's namespace would misstate provenance.
    assert MAPLE_MODEL_ID == "dealignai/deepseek-v4.1-flash-1017-uncensored"
    assert MAPLE_NATIVE_ID == "deepseek-v4-1-flash-1017-uncensored"
    assert "maple" not in model_vendor_provider_slugs(MAPLE_MODEL_ID)
    assert "deepseek" in model_vendor_provider_slugs("deepseek/deepseek-v4.1-flash")


@pytest.mark.parametrize("origin,admitted", [
    (None, False), ("", False), ("unknown-region", False), ("uaenorth", False),
    ("us-central1", True), ("aws-eu-west-3", True), ("azure-uaenorth", True),
])
def test_maple_authorization_does_not_infer_the_gateways_origin(origin, admitted):
    from trusted_router.authorize_candidates import CandidateRequest, plan_route, resolve_candidates
    from trusted_router.config import Settings

    request = CandidateRequest(
        route_type="chat.completions", route_model_id=MAPLE_MODEL_ID,
        routing_body={"model": MAPLE_MODEL_ID, "provider": {
            "only": ["maple"], "allow_fallbacks": False, "min_privacy": "confidential",
        }},
        body_region=origin,
    )
    plan = plan_route(request, Settings(environment="test"), region="us-central1")
    resolved = resolve_candidates(
        plan, request, region="us-central1", at=datetime(2026, 10, 29, tzinfo=UTC),
    )
    assert bool(resolved.candidates) is admitted
    if admitted:
        assert len(resolved.candidates) == 1
        endpoint = resolved.candidates[0][1]
        assert (endpoint.provider, endpoint.upstream_id, endpoint.usage_type) == (
            "maple", MAPLE_NATIVE_ID, "Credits",
        )


@pytest.mark.parametrize("region", [
    "us-central1", "us-east4", "europe-west4", "us-west1",
    "aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast", "",
])
def test_maple_requires_qualified_gateway_region(monkeypatch, region):
    from trusted_router import provider_contracts, routing_snapshot

    monkeypatch.setattr(provider_contracts, "MAPLE_GATEWAY_REGIONS", frozenset())
    monkeypatch.setattr(routing_snapshot, "MAPLE_GATEWAY_REGIONS", frozenset())
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="maple",
                       model_id=MAPLE_MODEL_ID, upstream_id=MAPLE_NATIVE_ID, usage_type="Credits")
    assert not provider_model_available_from_gateway_region("maple", MAPLE_MODEL_ID, region)
    snapshot = _gateway_regions(endpoint)
    assert snapshot.only == () and snapshot.requires_explicit_origin
    monkeypatch.setenv("TR_SERVING_REGION", region)
    endpoints = {endpoint.id: endpoint}
    apply_gateway_admission_holds({}, endpoints)
    assert not endpoints


def test_maple_unknown_model_never_inherits_qualified_regions(monkeypatch):
    from trusted_router import provider_contracts, routing_snapshot

    monkeypatch.setattr(provider_contracts, "MAPLE_GATEWAY_REGIONS", frozenset({"us-central1"}))
    monkeypatch.setattr(routing_snapshot, "MAPLE_GATEWAY_REGIONS", frozenset({"us-central1"}))
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="maple",
                       model_id="deepseek/deepseek-v4.1-flash", usage_type="Credits")
    assert not provider_model_available_from_gateway_region("maple", endpoint.model_id, "us-central1")
    assert _gateway_regions(endpoint).only == ()
    monkeypatch.setenv("TR_SERVING_REGION", "us-central1")
    endpoints = {endpoint.id: endpoint}
    apply_gateway_admission_holds({}, endpoints)
    assert not endpoints


@pytest.mark.parametrize("region", [
    "us-central1", "us-east4", "europe-west4", "us-west1",
    "aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast",
])
def test_qualified_maple_region_admits_only_reviewed_workload(monkeypatch, region):
    from trusted_router import provider_contracts, routing_snapshot

    qualified = frozenset({region})
    monkeypatch.setattr(provider_contracts, "MAPLE_GATEWAY_REGIONS", qualified)
    monkeypatch.setattr(routing_snapshot, "MAPLE_GATEWAY_REGIONS", qualified)
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="maple",
                       model_id=MAPLE_MODEL_ID, upstream_id=MAPLE_NATIVE_ID, usage_type="Credits")
    assert provider_model_available_from_gateway_region("maple", MAPLE_MODEL_ID, region)
    snapshot = _gateway_regions(endpoint)
    assert snapshot.only == (region,) and snapshot.requires_explicit_origin
    monkeypatch.setenv("TR_SERVING_REGION", region)
    endpoints = {endpoint.id: endpoint}
    apply_gateway_admission_holds({}, endpoints)
    assert endpoints == {endpoint.id: endpoint}
