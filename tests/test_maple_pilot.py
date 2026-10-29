from dataclasses import replace

import pytest

from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement
from trusted_router.provider_contracts import MAPLE_MODEL_ID, MAPLE_NATIVE_ID
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS


def test_maple_uses_only_the_measured_managed_adapter():
    provider = PROVIDERS["maple"]
    assert provider.supports_prepaid and not provider.supports_byok
    assert provider.attested_gateway and provider.provider_confidential_compute and provider.provider_e2ee
    assert provider.prepaid_zero_data_retention and not provider.provider_zero_data_retention
    assert "maple" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert "maple" not in OPENAI_COMPATIBLE_PROVIDERS


@pytest.mark.parametrize("variant", ["approved", "stock", "other-model", "wrong-native", "missing-native", "byok"])
def test_only_exact_managed_maple_route_qualifies_for_confidential(variant):
    endpoint = replace(next(iter(MODEL_ENDPOINTS.values())), provider="maple",
                       model_id=MAPLE_MODEL_ID, upstream_id=MAPLE_NATIVE_ID, usage_type="Credits")
    if variant == "stock":
        endpoint = replace(endpoint, model_id="deepseek/deepseek-v4.1-flash",
                           upstream_id="deepseek-v4-1-flash")
    elif variant == "other-model":
        endpoint = replace(endpoint, model_id="maple/future-model")
    elif variant == "wrong-native":
        endpoint = replace(endpoint, upstream_id="deepseek-v4-1-flash")
    elif variant == "missing-native":
        endpoint = replace(endpoint, upstream_id=None)
    elif variant == "byok":
        endpoint = replace(endpoint, usage_type="BYOK")
    for requirement in (PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION):
        assert endpoint_meets_privacy_requirement(endpoint, requirement) is (variant == "approved")


def test_maple_provider_page_explains_exact_scope(client):
    response = client.get("/providers/maple")
    assert response.status_code == 200
    for text in ("Maple", "Transport V2", "Unexpected measurements fail closed",
                 "other models or BYOK", "https://maple.ai/pricing"):
        assert text in response.text
