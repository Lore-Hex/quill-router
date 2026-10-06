"""Direct DeepSeek inventory must not inherit reseller-only model names."""

import json
from pathlib import Path

import pytest

from tests import catalog_vehicles
from trusted_router import catalog_ingest
from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_data import ModelEndpoint


@pytest.mark.parametrize("usage_type", ["Credits", "BYOK"])
def test_deepseek_endpoints_match_authenticated_manifest(usage_type: str) -> None:
    expected = catalog_ingest._authoritative_provider_model_ids("deepseek")
    actual = {
        endpoint.model_id for endpoint in catalog_vehicles.registry_endpoints().values()
        if endpoint.provider == "deepseek" and endpoint.usage_type == usage_type
    }
    assert actual == expected
    assert "deepseek/deepseek-v4.1-flash" not in actual


@pytest.mark.provider_health
@pytest.mark.parametrize("usage", ["prepaid", "byok"])
def test_deepseek_serves_rolling_flash_and_another_host_the_v41_release(usage: str) -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    assert f"deepseek/deepseek-flash@deepseek/{usage}" in MODEL_ENDPOINTS
    # Other providers' independently verified immutable release stays available.
    assert any(
        endpoint.model_id == "deepseek/deepseek-v4.1-flash"
        and endpoint.provider != "deepseek"
        for endpoint in MODEL_ENDPOINTS.values()
    )


@pytest.mark.parametrize("inventory", ["missing", "malformed", "future"])
def test_deepseek_inventory_is_fail_closed_and_accepts_new_verified_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inventory: str,
) -> None:
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    model_id = "deepseek/deepseek-future"
    if inventory == "malformed":
        (tmp_path / "deepseek.json").write_text("invalid", encoding="utf-8")
    elif inventory == "future":
        (tmp_path / "deepseek.json").write_text(json.dumps({"models": [{
            "id": model_id, "upstream_id": "deepseek-future", "model_type": "chat",
            "endpoints": ["chat/completions"],
        }]}), encoding="utf-8")
    endpoint = ModelEndpoint(id="future@deepseek/prepaid", model_id=model_id, provider="deepseek",
                             upstream_id="deepseek-future", usage_type="Credits")
    result = catalog_ingest._filter_unserved_provider_endpoints({endpoint.id: endpoint})
    assert bool(result) is (inventory == "future")
