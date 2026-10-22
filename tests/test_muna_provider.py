from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.pricing.providers import muna as p


def catalog(model="qwen/qwen-3.8-27b"):
    return {
        "object": "list", "contract_version": "2.0",
        "provider": {
            "id": "muna", "status_url": "https://status.muna.ai",
            "support_contact": "mailto:support@muna.ai", "incident_contact": "mailto:support@muna.ai",
            "regions": ["us"], "request_id_header": "x-request-id",
            "error_contract": {"rate_limit_status": 429, "overload_status": 503,
                               "retry_after_header": "Retry-After", "account_quota_error_codes": ["insufficient_quota"]},
        },
        "data": [{
            "id": model, "object": "model", "owned_by": "qwen", "name": "Qwen 3.8 27B",
            "type": "chat", "context_length": 262144, "max_output_tokens": 65536,
            "endpoints": ["chat/completions"], "input_modalities": ["text", "image"], "output_modalities": ["text"],
            "capabilities": {"streaming": True, "tools": True, "structured_output": False,
                             "reasoning": True, "prompt_caching": True},
            "pricing": {"currency": "USD", "unit": "per_1m_tokens", "input": "0.25", "output": "1.75",
                        "cached_input": "0.02", "cache_write": None, "minimum_request": "0"},
            "lifecycle": {"status": "active", "deprecation_at": None, "retirement_at": None,
                          "replacement_model_id": None},
            "reliability": {"first_token_timeout_seconds": 60, "completion_timeout_seconds": 600,
                            "stream_idle_timeout_seconds": 30, "capacity_scope": "model"},
        }],
    }


def test_exact_prices_native_identity_and_capabilities():
    prices, rows = p.discover(catalog())
    price = prices["qwen/qwen3.8-27b"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (250000, 1750000)
    assert price.tiers[0].prompt_cached_micro_per_m == 20000
    row = rows["qwen/qwen3.8-27b"]
    assert row["upstream_id"] == "qwen/qwen-3.8-27b"
    assert row["input_modalities"] == ["text", "image"]
    assert "prompt_caching" in row["supported_features"]
    assert "structured_outputs" not in row["supported_features"]
    assert "tool_choice" not in row.get("supported_parameters", [])
    assert "support_contact" not in json.dumps(rows)
    assert "incident_contact" not in json.dumps(rows)
    assert row["reliability"]["first_token_timeout_seconds"] == 60


def test_discovery_is_not_a_static_allowlist_and_maps_lifecycle():
    prices, _ = p.discover(catalog("muna/future-model"))
    assert "muna/future-model" in prices
    payload = catalog()
    retired = copy.deepcopy(payload["data"][0])
    retired["id"] = "muna/retired"
    retired["lifecycle"]["status"] = "retired"
    payload["data"].append(retired)
    payload["data"][0]["lifecycle"]["replacement_model_id"] = "qwen/qwen-3.8-27b"
    _, rows = p.discover(payload)
    assert "muna/retired" not in rows
    assert rows["qwen/qwen3.8-27b"]["replacement_model_id"] == "qwen/qwen3.8-27b"


@pytest.mark.parametrize("field,value", [("input", "NaN"), ("input", "-1"),
    ("currency", "EUR"), ("cached_input", "0.3"), ("minimum_request", "1"), ("cache_write", "0.1")])
def test_invalid_pricing_fails_closed(field, value):
    payload = catalog()
    payload["data"][0]["pricing"][field] = value
    with pytest.raises(RuntimeError):
        p.discover(payload)


def test_empty_wrong_provider_and_duplicate_canonical_catalog_rejected():
    payload = catalog()
    payload["data"] = []
    with pytest.raises(RuntimeError):
        p.discover(payload)
    payload = catalog()
    payload["provider"]["id"] = "someone-else"
    with pytest.raises(RuntimeError, match="unexpected catalog provider"):
        p.discover(payload)
    payload = catalog()
    del payload["contract_version"]
    with pytest.raises(RuntimeError, match="Contract v2"):
        p.discover(payload)
    payload = catalog()
    other = copy.deepcopy(payload["data"][0])
    other["id"] = "qwen/qwen3.8-27b"
    payload["data"].append(other)
    with pytest.raises(RuntimeError, match="duplicate canonical"):
        p.discover(payload)


def test_canary_failure_recovery_and_failed_fetch_cannot_republish(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "muna.json")
    monkeypatch.setenv("MUNA_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(p, "probe_openai_chat", lambda **kw: calls.append(kw) or False)
    result = p.fetch()
    p.write_provider_manifest(result)
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False and row["routable_reason"] == "provider-canary-failed"
    assert calls[0]["model"] == "qwen/qwen-3.8-27b"
    assert calls[0]["require_usage"] and calls[0]["expected_content"] == "PONG"
    monkeypatch.setattr(p, "probe_openai_chat", lambda **kw: True)
    result = p.fetch()
    p.write_provider_manifest(result)
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] and "routable_reason" not in row
    monkeypatch.delenv("MUNA_API_KEY")
    with pytest.raises(RuntimeError, match="required"):
        p.fetch()
    assert not p.UPSTREAM_ID_MAP
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        p.write_provider_manifest(result)


def test_registration_privacy_secret_wiring_and_routes():
    from scripts.check_price_coverage import (
        _DISCOVERABLE_MANIFEST_PROVIDERS,
        _STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG,
    )
    from scripts.pricing.refresh import PROVIDER_SLUGS
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
    from trusted_router.provider_locations import PROVIDER_INFERENCE_LOCATIONS
    from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

    provider = PROVIDERS[p.SLUG]
    assert provider.stores_content and not provider.provider_zero_data_retention
    assert not provider.prepaid_zero_data_retention
    assert not provider.provider_e2ee and not provider.provider_confidential_compute
    assert not provider.renewable_energy_inference and provider.provider_headquarters_country == "US"
    assert p.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS and p.SLUG in PROVIDER_SLUGS
    assert p.SLUG in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert _STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG[p.SLUG] is p
    assert OPENAI_COMPATIBLE_PROVIDERS[p.SLUG] == (("MUNA_API_KEY",), p.BASE_URL)
    assert any(item[0] == p.SLUG for item in _DISCOVERABLE_MANIFEST_PROVIDERS)
    assert PROVIDER_INFERENCE_LOCATIONS[p.SLUG].locations == ("United States",)
    root = Path(__file__).resolve().parents[1]
    assert "MUNA_API_KEY:trustedrouter-muna-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "MUNA_API_KEY" "trustedrouter-muna-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()
    for model_id, native in (("qwen/qwen3.8-27b", "qwen/qwen-3.8-27b"),
                             ("google/gemma-4-26b-a4b-it", "google/gemma-4-26b-a4b-it")):
        endpoint = MODEL_ENDPOINTS[f"{model_id}@muna/prepaid"]
        assert endpoint.upstream_id == native and endpoint.usage_type == "Credits"
        assert "tools" in endpoint.supported_parameters
        assert "tool_choice" not in endpoint.supported_parameters
        assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens is not None
        assert f"{model_id}@muna/byok" not in MODEL_ENDPOINTS


def test_public_page_includes_cache_and_retention_exception(client):
    response = client.get("/providers/muna")
    assert response.status_code == 200
    for text in ("Muna", "qwen3.8-27b", "Cached", "30 days", "https://muna.ai/zdr", "United States"):
        assert text in response.text
    assert "yusuf@muna.ai" not in response.text
