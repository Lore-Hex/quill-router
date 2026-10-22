from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.pricing.providers import _direct_openai
from scripts.pricing.providers import coralbricks as p
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider


def row(native="deepseek-v4.1-flash-fast"):
    return {
        "id": native, "context_length": 1048576,
        "supports_chat": True, "supports_tools": True,
        "supports_reasoning": True, "supports_image_input": native.startswith("deepseek"),
        "reasoning": {"supported_efforts": ["low", "high", "max"]},
        "pricing": {"input_per_m": "0.01", "output_per_m": "1.20",
                    "cached_input_per_m": 0, "cache_write_per_m": "0.08"},
    }


def test_discovery_preserves_units_free_cache_native_id_and_capabilities(monkeypatch, tmp_path):
    monkeypatch.setenv("CORALBRICKS_API_KEY", "fixture-key")
    requests = []
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda url, **kw: {"data": [row(), row("glm-5.3-fast"), row("future-chat")]})
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: requests.append(kw) or True)
    adapter = DirectOpenAIProvider(p.CATALOG.spec, manifest_path=tmp_path / "catalog.json")
    result = adapter.fetch()
    price = result.prices["deepseek/deepseek-v4.1-flash"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (10000, 1200000)
    assert price.tiers[0].prompt_cached_micro_per_m == 0
    assert "coralbricks/future-chat" in result.prices
    adapter.write_provider_manifest(result)
    rows = {item["id"]: item for item in json.loads(adapter.manifest_path.read_text())["models"]}
    deepseek = rows["deepseek/deepseek-v4.1-flash"]
    assert deepseek["cached_input_token_price_per_m"] == 0
    assert deepseek["upstream_id"] == "deepseek-v4.1-flash-fast"
    assert deepseek["context_length"] == 1048576
    assert deepseek["input_modalities"] == ["text", "image"]
    assert rows["z-ai/glm-5.3"]["input_modalities"] == ["text"]
    assert "tools" in deepseek["supported_features"]
    assert "reasoning-effort" in deepseek["supported_features"]
    for request in requests:
        assert request["base_url"] == p.BASE_URL
        assert request["require_usage"] and request["require_message"]
        assert request["extra_body"]["prompt_cache_retention"] == "off"


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-1", True, "bad"])
@pytest.mark.parametrize("field", ["input_per_m", "output_per_m", "cached_input_per_m", "cache_write_per_m"])
def test_invalid_or_missing_prices_fail_closed(field, value):
    payload = row()
    payload["pricing"][field] = value
    with pytest.raises(RuntimeError):
        p.normalize_rows([payload])


def test_normalization_does_not_mutate_source_and_rejects_duplicates():
    payload = row()
    original = copy.deepcopy(payload)
    p.normalize_rows([payload])
    assert payload == original
    with pytest.raises(RuntimeError, match="duplicate"):
        p.normalize_rows([payload, payload])


def test_failed_canary_is_held_and_failed_fetch_cannot_publish(monkeypatch, tmp_path):
    monkeypatch.setenv("CORALBRICKS_API_KEY", "fixture-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *a, **kw: {"data": [row()]})
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: False)
    adapter = DirectOpenAIProvider(p.CATALOG.spec, manifest_path=tmp_path / "catalog.json")
    result = adapter.fetch()
    adapter.write_provider_manifest(result)
    published = json.loads(adapter.manifest_path.read_text())["models"][0]
    assert published["routable"] is False
    monkeypatch.delenv("CORALBRICKS_API_KEY")
    with pytest.raises(RuntimeError):
        adapter.fetch()
    with pytest.raises(RuntimeError):
        adapter.write_provider_manifest(result)


def test_publication_privacy_prices_and_hourly_refresh(client):
    from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
    from trusted_router.catalog_privacy import PRIVACY_TIER_STANDARD, provider_privacy_tier
    from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars

    provider = PROVIDERS[p.SLUG]
    # Provider defaults are distinct from our managed organization's opt-in.
    assert provider_privacy_tier(provider) == PRIVACY_TIER_STANDARD
    assert provider.prepaid_zero_data_retention is True
    assert provider.prepaid_zero_data_retention_effective_on == "2026-10-14"
    assert not provider.provider_e2ee and not provider.provider_confidential_compute
    assert not provider.renewable_energy_inference
    assert provider.provider_headquarters_country == "US"
    endpoint = MODEL_ENDPOINTS["deepseek/deepseek-v4.1-flash@coralbricks/prepaid"]
    assert endpoint.usage_type == "Credits"
    assert "reasoning_effort" in endpoint.supported_parameters
    assert endpoint.input_modalities == ("text", "image")
    assert "z-ai/glm-5.3@coralbricks/prepaid" not in MODEL_ENDPOINTS
    assert endpoint.prompt_price_microdollars_per_million_tokens == 10550
    assert endpoint.completion_price_microdollars_per_million_tokens == 1266000
    # Zero-cost upstream cache reads retain the platform's published $0.01/M floor.
    assert _endpoint_cost_microdollars(endpoint, 0, 0, cache_read_tokens=1_000_000) == 10000
    assert _endpoint_cost_microdollars(endpoint, 1_000_000, 1_000_000) == 1276550
    response = client.get("/providers/coralbricks")
    assert response.status_code == 200
    assert "CoralBricks" in response.text and "coralbricks.png" in response.text
    assert "paid cache retention" in response.text
    assert "No content + no metadata" in response.text
    assert "TR-funded routes" in response.text
    assert "ZDR" in response.text
    assert "Operational counters and billing records remain" in response.text
    assert "routes remain Standard" not in response.text
    assert "divy@" not in response.text and "hitesh@" not in response.text
    root = Path(__file__).resolve().parents[1]
    assert "CORALBRICKS_API_KEY:trustedrouter-coralbricks-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()


def test_managed_zdr_scope_and_routing_do_not_imply_confidential_or_byok():
    model_id = "deepseek/deepseek-v4.1-flash"
    from trusted_router.catalog import (
        MODEL_ENDPOINTS,
        MODELS,
        PROVIDERS,
        model_to_openrouter_shape,
        provider_to_openrouter_shape,
    )
    from trusted_router.catalog_privacy import (
        PRIVACY_TIER_CONFIDENTIAL,
        PRIVACY_TIER_ZERO_RETENTION,
        endpoint_meets_privacy_requirement,
        endpoint_stores_content,
        endpoint_zero_data_retention_scope,
    )
    from trusted_router.config import Settings
    from trusted_router.routing import chat_route_endpoint_candidates

    endpoint = MODEL_ENDPOINTS[f"{model_id}@coralbricks/prepaid"]
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
    assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)
    assert not endpoint_stores_content(endpoint)
    assert endpoint_zero_data_retention_scope(endpoint) == "trustedrouter_prepaid"

    # No BYOK route is published, and a future one must not inherit our opt-in.
    byok = replace(endpoint, usage_type="BYOK")
    assert not endpoint_meets_privacy_requirement(byok, PRIVACY_TIER_ZERO_RETENTION)
    assert endpoint_stores_content(byok)
    assert endpoint_zero_data_retention_scope(byok) is None

    provider_shape = provider_to_openrouter_shape(PROVIDERS[p.SLUG])
    assert provider_shape["provider_zero_data_retention"] is False
    assert provider_shape["prepaid_zero_data_retention"] is True
    shape = model_to_openrouter_shape(MODELS[model_id])
    routes = [item for item in shape["trustedrouter"]["endpoints"] if item["provider"] == p.SLUG]
    assert routes
    assert all(item["usage_type"] == "Credits" for item in routes)
    assert all(item["provider_zero_data_retention"] is True for item in routes)
    assert all(item["zero_data_retention_scope"] == "trustedrouter_prepaid" for item in routes)

    candidates = chat_route_endpoint_candidates(
        {"model": model_id, "provider": {"only": [p.SLUG], "allow_fallbacks": False, "min_privacy": "zdr"}},
        Settings(environment="test"),
    )
    assert candidates
    assert all(route.provider == p.SLUG and route.usage_type == "Credits" for _, route in candidates)


def test_discovery_and_stale_policy_are_registered_once():
    from scripts.check_price_coverage import (
        _DISCOVERABLE_MANIFEST_PROVIDERS,
        _STALE_MANIFEST_PROVIDER_MODULES,
    )
    from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS

    discovery = [entry for entry in _DISCOVERABLE_MANIFEST_PROVIDERS if entry[0] == p.SLUG]
    assert len(discovery) == 1
    assert discovery[0][1:3] == (p.URL, ("CORALBRICKS_API_KEY",))
    assert sum(module.SLUG == p.SLUG for module in _STALE_MANIFEST_PROVIDER_MODULES) == 1
    assert p.SLUG in EXPIRING_PROVIDER_MANIFEST_SLUGS
