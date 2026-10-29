from dataclasses import replace

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_data import model_vendor_provider_slugs
from trusted_router.catalog_deployment import apply_gateway_admission_holds
from trusted_router.provider_contracts import (
    MAPLE_MODEL_ID,
    MAPLE_NATIVE_ID,
    provider_model_available_from_gateway_region,
)
from trusted_router.routing_snapshot import _gateway_regions


def test_maple_checkpoint_names_its_weights_author_not_host():
    # The pinned i02 workload uses dealignai's immutable uncensored checkpoint.
    # Renaming a stock model into the host's namespace would misstate provenance.
    assert MAPLE_MODEL_ID == "dealignai/deepseek-v4.1-flash-1017-uncensored"
    assert MAPLE_NATIVE_ID == "deepseek-v4-1-flash-1017-uncensored"
    assert "maple" not in model_vendor_provider_slugs(MAPLE_MODEL_ID)
    assert "deepseek" in model_vendor_provider_slugs("deepseek/deepseek-v4.1-flash")


@pytest.mark.parametrize("region", [
    "us-central1", "us-east4", "europe-west4", "us-west1",
    "aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast", "",
])
def test_maple_requires_qualified_gateway_region(monkeypatch, region):
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
