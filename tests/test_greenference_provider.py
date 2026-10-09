from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.pricing.providers import greenference as p


def catalog(model="greenference/glm-5.3-flash"):
    return {"object": "list", "data": [{
        "id": model, "object": "model", "owned_by": "greenference", "name": "GLM",
        "type": "chat", "context_length": 1048576, "max_output_tokens": 8192,
        "endpoints": ["chat/completions"], "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "capabilities": {"streaming": True, "tools": True, "structured_output": True,
                         "reasoning": True, "prompt_caching": True},
        "pricing": {"currency": "USD", "unit": "per_1m_tokens", "input": "0.14",
                    "output": "0.49", "cached_input": "0.028", "cache_write": None,
                    "minimum_request": "0"},
        "lifecycle": {"status": "active", "deprecation_at": None, "retirement_at": None,
                      "replacement_model_id": None},
    }]}


def test_exact_prices_native_identity_and_capabilities():
    prices, rows = p.discover(catalog())
    price = prices["z-ai/glm-5.3-flash"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (140000, 490000)
    assert price.tiers[0].prompt_cached_micro_per_m == 28000
    row = rows["z-ai/glm-5.3-flash"]
    assert row["upstream_id"] == "greenference/glm-5.3-flash"
    assert row["input_modalities"] == ["text", "image"]
    assert "prompt_caching" in row["supported_features"]


def test_new_model_discovered_without_hardcoded_allowlist():
    prices, rows = p.discover(catalog("greenference/future-model"))
    assert "greenference/future-model" in prices
    assert rows["greenference/future-model"]["upstream_id"] == "greenference/future-model"


def test_retired_models_are_not_published():
    payload = catalog()
    retired = copy.deepcopy(payload["data"][0])
    retired["id"] = "greenference/retired"
    retired["lifecycle"]["status"] = "retired"
    payload["data"].append(retired)
    assert "greenference/retired" not in p.discover(payload)[1]


@pytest.mark.parametrize("field,value", [("input", "NaN"), ("input", "-1"),
    ("currency", "EUR"), ("cached_input", "0.2"), ("minimum_request", "1")])
def test_invalid_pricing_fails_closed(field, value):
    payload = catalog()
    payload["data"][0]["pricing"][field] = value
    with pytest.raises(RuntimeError):
        p.discover(payload)


def test_empty_and_duplicate_catalog_rejected():
    with pytest.raises(RuntimeError):
        p.discover({"object": "list", "data": []})
    payload = catalog()
    payload["data"].append(copy.deepcopy(payload["data"][0]))
    with pytest.raises(RuntimeError):
        p.discover(payload)


def test_canaries_native_id_usage_and_failed_fetch_cannot_republish(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "greenference.json")
    monkeypatch.setenv("GREENFERENCE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    calls = []
    monkeypatch.setattr(p, "probe_openai_chat", lambda **kw: calls.append(kw) or False)
    result = p.fetch()
    p.write_provider_manifest(result)
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"
    assert calls[0]["model"] == "greenference/glm-5.3-flash"
    assert calls[0]["require_usage"] is True
    monkeypatch.delenv("GREENFERENCE_API_KEY")
    with pytest.raises(RuntimeError, match="required"):
        p.fetch()
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        p.write_provider_manifest(result)


def test_registration_privacy_and_secret_wiring():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
    from trusted_router.provider_locations import PROVIDER_INFERENCE_LOCATIONS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

    provider = PROVIDERS[p.SLUG]
    assert provider.provider_zero_data_retention and not provider.stores_content
    assert not provider.provider_e2ee and not provider.provider_confidential_compute
    assert not provider.renewable_energy_inference
    assert provider.provider_headquarters_country == "FR"
    assert p.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert OPENAI_COMPATIBLE_PROVIDERS[p.SLUG] == (("GREENFERENCE_API_KEY",), p.BASE_URL)
    assert any(item[0] == p.SLUG for item in _DISCOVERABLE_MANIFEST_PROVIDERS)
    assert "European Union" in PROVIDER_INFERENCE_LOCATIONS[p.SLUG].locations[0]
    root = Path(__file__).resolve().parents[1]
    assert "GREENFERENCE_API_KEY:trustedrouter-greenference-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "GREENFERENCE_API_KEY" "trustedrouter-greenference-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()


def test_published_manifest_redacts_contacts_and_preserves_native_routes():
    from trusted_router.catalog import MODEL_ENDPOINTS

    raw = json.loads((Path(__file__).resolve().parents[1] / "src/trusted_router/data/provider_models/greenference.json").read_text())
    assert len(raw["models"]) >= 10
    assert "support_contact" not in json.dumps(raw)
    assert "incident_contact" not in json.dumps(raw)
    for row in raw["models"]:
        endpoint = MODEL_ENDPOINTS[row["id"] + "@greenference/prepaid"]
        assert endpoint.upstream_id == row["upstream_id"]
        assert endpoint.usage_type == "Credits"


def test_public_provider_page_and_cache_pricing(client):
    from trusted_router.catalog import MODEL_ENDPOINTS

    response = client.get("/providers/greenference")
    assert response.status_code == 200
    assert "Greenference" in response.text
    assert "glm-5.3-flash" in response.text
    assert "https://greenference.com/legal/dpa" in response.text
    assert "greenference.png" in response.text
    assert "Cached" in response.text
    endpoint = MODEL_ENDPOINTS["z-ai/glm-5.3-flash@greenference/prepaid"]
    cached = endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens
    assert f"${cached / 1_000_000:g}/1M" in response.text
    assert "support_contact" not in response.text
    assert "incident_contact" not in response.text
