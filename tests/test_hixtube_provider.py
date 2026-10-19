from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.providers import hixtube as p


def catalog():
    return {"object": "list", "contract_version": "2.0", "provider": {
        "id": "hixtube", "status_url": "https://api.hixtube.com/healthz",
        "support_contact": "mailto:support@trustedrouter.com",
        "incident_contact": "mailto:support@trustedrouter.com",
        "regions": ["US"], "request_id_header": "X-Request-Id",
        "error_contract": {"rate_limit_status": 429, "overload_status": 503,
                           "retry_after_header": "Retry-After", "account_quota_error_codes": []},
    }, "data": [{
        "id": "qwen/qwen3-0.6b", "object": "model", "owned_by": "qwen",
        "name": "Qwen3 0.6B (Hixtube CPU pilot)", "type": "chat",
        "context_length": 4096, "max_output_tokens": 256,
        "endpoints": ["chat/completions"], "input_modalities": ["text"], "output_modalities": ["text"],
        "capabilities": {"streaming": True, "tools": False, "reasoning": False,
                         "structured_output": False, "prompt_caching": False},
        "pricing": {"currency": "USD", "unit": "per_1m_tokens", "input": "0.10", "output": "0.10",
                    "cached_input": None, "cache_write": None, "minimum_request": "0"},
        "lifecycle": {"status": "active", "deprecation_at": None, "retirement_at": None,
                      "replacement_model_id": None},
        "reliability": {"first_token_timeout_seconds": 30, "completion_timeout_seconds": 120,
                        "stream_idle_timeout_seconds": 30, "capacity_scope": "model"},
    }]}


def test_catalog_preserves_price_capacity_and_native_identity():
    prices, rows = p.discover(catalog())
    price = prices["qwen/qwen3-0.6b"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (100000, 100000)
    row = rows["qwen/qwen3-0.6b"]
    assert row["upstream_id"] == "qwen/qwen3-0.6b"
    assert row["context_length"] == 4096
    assert row["max_output_tokens"] == 256
    assert "vision" not in row["supported_features"]
    reliability = row["provider_reliability"]
    assert reliability["provider_id"] == "hixtube"
    assert reliability["regions"] == ["US"]
    assert row["reliability"]["completion_timeout_seconds"] == 120
    assert "support_contact" not in reliability and "incident_contact" not in reliability


def test_catalog_v2_contract_requires_full_reliability():
    payload = catalog()
    del payload["data"][0]["reliability"]["capacity_scope"]
    with pytest.raises(RuntimeError, match="capacity_scope"):
        p.discover(payload)


def test_successful_v2_fetch_publishes_only_canary_verified_route(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(p, "probe_openai_chat", lambda **_kw: True)
    result = p.fetch()
    p.write_provider_manifest(result)
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is True
    assert row["upstream_id"] == "qwen/qwen3-0.6b"
    assert row["max_output_tokens"] == 256
    assert row["provider_reliability"]["provider_id"] == "hixtube"


@pytest.mark.parametrize("value", ["NaN", "-1", None, 0.1])
def test_invalid_price_cannot_publish(value):
    payload = catalog()
    payload["data"][0]["pricing"]["input"] = value
    with pytest.raises(RuntimeError):
        p.discover(payload)


def test_missing_key_failed_probe_and_stale_fetch_are_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "hixtube.json")
    monkeypatch.setenv("HIXTUBE_API_KEY", "test-key")
    monkeypatch.setattr(p, "fetch_json", lambda *_a, **_kw: catalog())
    calls = []
    monkeypatch.setattr(p, "probe_openai_chat", lambda **kw: calls.append(kw) or False)
    result = p.fetch()
    p.write_provider_manifest(result)
    row = json.loads(p.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"
    assert calls[0]["require_usage"] is True and calls[0]["max_tokens"] <= 256
    monkeypatch.delenv("HIXTUBE_API_KEY")
    with pytest.raises(RuntimeError, match="required"):
        p.fetch()
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        p.write_provider_manifest(result)


def test_registration_never_claims_confidentiality():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

    provider = PROVIDERS[p.SLUG]
    assert provider.supports_prepaid and not provider.supports_byok
    assert not provider.provider_zero_data_retention
    assert not provider.provider_e2ee and not provider.provider_confidential_compute
    assert p.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert OPENAI_COMPATIBLE_PROVIDERS[p.SLUG] == (("HIXTUBE_API_KEY",), p.BASE_URL)
    assert any(row[0] == p.SLUG for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
    root = Path(__file__).resolve().parents[1]
    assert "HIXTUBE_API_KEY:trustedrouter-hixtube-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "HIXTUBE_API_KEY" "trustedrouter-hixtube-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()
