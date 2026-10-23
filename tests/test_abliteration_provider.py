from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.pricing.providers import _direct_openai
from scripts.pricing.providers import abliteration as p


def catalog(native="abliterated-model"):
    return {"data": [{
        "id": native, "name": "Abliterated Model", "context_length": 262144,
        "max_output_length": 262134, "input_modalities": ["text", "image"],
        "output_modalities": ["text"], "is_ready": True,
        "access": {"available": True, "locked": False},
        "pricing": {"prompt": "0.000001", "completion": "0.000003",
                    "input_cache_read": "0.0000001", "request": "0", "image": "0"},
        "supported_features": ["tools", "reasoning", "structured_outputs", "web_search"],
    }]}


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    instance = _direct_openai.DirectOpenAIProvider(p.CATALOG.spec, manifest_path=tmp_path / "abliteration.json")
    monkeypatch.setenv("ABLITERATION_API_KEY", "test-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: True)
    return instance


def test_exact_token_and_cache_prices_and_capabilities(adapter):
    result = adapter.fetch()
    price = result.prices["abliteration/abliterated-model"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (1000000, 3000000)
    assert price.tiers[0].prompt_cached_micro_per_m == 100000
    row = adapter.discovered_rows["abliteration/abliterated-model"]
    assert row["upstream_id"] == "abliterated-model"
    assert row["context_length"] == 262144 and row["max_output_tokens"] == 262134
    assert row["input_modalities"] == ["text", "image"]
    assert "prompt_caching" in row["supported_features"]
    assert "web_search" not in row["supported_features"]


def test_future_models_auto_discovered_and_unavailable_models_removed(adapter, monkeypatch):
    payload = catalog("future-model-v9")
    for field, value in (("available", False), ("locked", True)):
        unavailable = copy.deepcopy(payload["data"][0])
        unavailable["id"] = f"unavailable-{field}"
        unavailable["access"][field] = value
        payload["data"].append(unavailable)
    not_ready = copy.deepcopy(payload["data"][0])
    not_ready.update(id="not-ready", is_ready=False)
    payload["data"].append(not_ready)
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *_a, **_kw: payload)
    assert set(adapter.fetch().prices) == {"abliteration/future-model-v9"}


@pytest.mark.parametrize("field,value", [
    ("prompt", "NaN"), ("prompt", "-1"), ("completion", "0"),
    ("input_cache_read", "Infinity"), ("input_cache_read", "0.001"),
    ("prompt", "0.0000000000001"), ("request", "0.01"), ("image", "0.01"),
    ("input_cache_write", "0.01"), ("web_search", "0.01"),
    ("currency", "EUR"), ("unit", "per_1m_tokens"),
])
def test_invalid_or_unmetered_price_rejected(field, value):
    payload = catalog()
    payload["data"][0]["pricing"][field] = value
    with pytest.raises(RuntimeError):
        p.normalize_rows(payload["data"])


@pytest.mark.parametrize("field", ["pricing", "access"])
def test_missing_declaration_rejected(field):
    row = catalog()["data"][0]
    del row[field]
    with pytest.raises(RuntimeError):
        p.normalize_rows([row])


def test_invalid_duplicate_ids_and_missing_price_rejected():
    rows = catalog()["data"]
    with pytest.raises(RuntimeError, match="duplicate"):
        p.normalize_rows(rows * 2)
    assert p.canonical_model_id("abliterated-model-large-v2") == "abliteration/abliterated-model-large-v2"
    assert p.canonical_model_id("openai/gpt-6") is None
    with pytest.raises(RuntimeError, match="invalid"):
        p.normalize_rows(catalog("openai/gpt-6")["data"])
    del rows[0]["pricing"]["input_cache_read"]
    with pytest.raises(RuntimeError, match="price"):
        p.normalize_rows(rows)


def test_canary_usage_required_failure_recovery_and_failed_fetch(adapter, monkeypatch):
    calls = []
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: calls.append(kw) or False)
    result = adapter.fetch()
    adapter.write_provider_manifest(result)
    row = json.loads(adapter.manifest_path.read_text())["models"][0]
    assert row["routable"] is False and row["routable_reason"] == "provider-canary-failed"
    assert calls[0]["model"] == "abliterated-model"
    assert calls[0]["require_usage"] and calls[0]["require_message"]
    assert calls[0]["expected_content"] == "PONG"
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: True)
    result = adapter.fetch()
    adapter.write_provider_manifest(result)
    assert json.loads(adapter.manifest_path.read_text())["models"][0]["routable"]
    monkeypatch.delenv("ABLITERATION_API_KEY")
    with pytest.raises(RuntimeError, match="required"):
        adapter.fetch()
    with pytest.raises(RuntimeError):
        adapter.write_provider_manifest(result)


def test_provider_is_registered_separately_and_cloud_secret_wired():
    from scripts.check_price_coverage import (
        _DISCOVERABLE_MANIFEST_PROVIDERS,
        _STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG,
    )
    from scripts.pricing.refresh import PROVIDER_SLUGS
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
    from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
    from trusted_router.sentry_config import SENSITIVE_STRING_FRAGMENTS

    provider = PROVIDERS[p.SLUG]
    assert not provider.stores_content and provider.provider_zero_data_retention
    assert not provider.provider_e2ee and not provider.provider_confidential_compute
    assert not provider.supports_byok
    assert p.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS and p.SLUG in PROVIDER_SLUGS
    assert p.SLUG in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert _STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG[p.SLUG] is p
    assert any(item[0] == p.SLUG for item in _DISCOVERABLE_MANIFEST_PROVIDERS)
    assert OPENAI_COMPATIBLE_PROVIDERS[p.SLUG] == (("ABLITERATION_API_KEY",), p.BASE_URL)
    assert OPENAI_COMPATIBLE_PROVIDERS["abliterate"] != OPENAI_COMPATIBLE_PROVIDERS[p.SLUG]
    assert "abliteration_api_key" in SENSITIVE_STRING_FRAGMENTS
    root = Path(__file__).resolve().parents[1]
    assert "ABLITERATION_API_KEY:trustedrouter-abliteration-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "ABLITERATION_API_KEY" "trustedrouter-abliteration-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()
    routes = [ep for ep in MODEL_ENDPOINTS.values() if ep.provider == p.SLUG]
    assert len(routes) == 4
    assert all(ep.usage_type == "Credits" for ep in routes)
    assert all(ep.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens is not None for ep in routes)


def test_provider_page(client):
    response = client.get("/providers/abliteration")
    assert response.status_code == 200
    for text in ("Abliteration", "abliterated-model-large-flash", "Cached", "https://abliteration.ai/data-handling"):
        assert text in response.text
