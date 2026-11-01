from datetime import UTC, datetime

import pytest

from tests import catalog_vehicles
from trusted_router import catalog
from trusted_router.provider_lifecycle import provider_model_retired
from trusted_router.synthetic.probes import rotation_candidates


def test_databricks_inkling_retirement_is_exact_and_effective_dated():
    model = "thinkingmachines/inkling"
    native = "databricks-inkling"
    assert not provider_model_retired("databricks", model, native, at="2026-10-29T23:59:59Z")
    assert provider_model_retired("databricks", model, native, at="2026-10-30T00:00:00Z")
    assert provider_model_retired("databricks", "unknown-alias", native, at="2026-10-31T00:00:00Z")
    assert not provider_model_retired("nvidia-nim", model, native, at=datetime(2026, 11, 1, tzinfo=UTC))
    assert not provider_model_retired("databricks", "moonshotai/kimi-k3", "databricks-kimi-k3")
    assert not provider_model_retired("databricks", "z-ai/glm-5.2", "databricks-glm-5-2")


def test_retired_databricks_inkling_is_not_offered_or_probed():
    model = "thinkingmachines/inkling"
    assert not any(endpoint.provider == "databricks" for endpoint in catalog.endpoints_for_model(model))
    assert model not in rotation_candidates().get("databricks", [])


@pytest.mark.parametrize("provider,model", [
    ("streamlake", "xiaomi/mimo-v2.5-pro"),
    ("chutes", "mistralai/mistral-nemo"),
])
def test_confirmed_failing_routes_are_not_managed_or_rotated(provider, model):
    endpoints = [endpoint for endpoint in catalog_vehicles.registry_endpoints().values()
                 if endpoint.model_id == model and endpoint.usage_type == "Credits"]
    assert not any(endpoint.provider == provider for endpoint in endpoints)
    if provider == "chutes":
        assert endpoints == []
    else:
        assert any(endpoint.provider != provider for endpoint in endpoints)
    assert model not in rotation_candidates().get(provider, [])
