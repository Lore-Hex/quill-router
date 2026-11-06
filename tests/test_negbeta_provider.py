from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.pricing.providers import negbeta as p

QWEN = "qwen/qwen3.8-27b"
DEEPSEEK = "deepseek/deepseek-v4.1-flash-1017"


def catalog():
    return {
        "object": "list", "contract_version": "2.0",
        "provider": {
            "id": "negbeta", "status_url": "https://inference.negbeta.com/status",
            "support_contact": "mailto:private@example.test", "incident_contact": "mailto:private@example.test",
            "regions": ["eu"], "request_id_header": "X-Request-Id",
            "error_contract": {"rate_limit_status": 429, "overload_status": 503,
                               "retry_after_header": "Retry-After", "account_quota_error_codes": ["insufficient_quota"]},
        },
        "data": [{
            "id": model, "object": "model", "owned_by": model.split("/")[0], "name": model,
            "type": "chat", "context_length": context, "max_output_tokens": output_limit,
            "endpoints": ["chat/completions"], "input_modalities": ["text", "image"], "output_modalities": ["text"],
            "capabilities": {"streaming": True, "tools": True, "structured_output": True,
                             "reasoning": True, "prompt_caching": True},
            "pricing": {"currency": "USD", "unit": "per_1m_tokens", "input": input_price,
                        "output": output_price, "cached_input": cache, "cache_write": None, "minimum_request": "0"},
            "lifecycle": {"status": "active", "deprecation_at": None, "retirement_at": None,
                          "replacement_model_id": None},
            "reliability": {"first_token_timeout_seconds": 120, "completion_timeout_seconds": 900,
                            "stream_idle_timeout_seconds": 30, "capacity_scope": "model"},
        } for model, context, output_limit, input_price, output_price, cache in (
            (QWEN, 262144, 65536, "0.19", "1.95", "0.045"),
            (DEEPSEEK, 1048576, 393216, "0.30", "1.20", "0.03"),
        )],
    }


def test_exact_prices_native_ids_and_route_specific_limits():
    prices, rows = p.discover(catalog())
    assert set(rows) == {QWEN, DEEPSEEK}
    for model, amounts, limits in (
        (QWEN, (190000, 1950000, 45000), (262144, 65536)),
        (DEEPSEEK, (300000, 1200000, 30000), (1048576, 393216)),
    ):
        price, row = prices[model], rows[model]
        assert (price.prompt_micro_per_m, price.completion_micro_per_m,
                price.tiers[0].prompt_cached_micro_per_m) == amounts
        assert row["upstream_id"] == p.canonical_model_id(model) == model
        assert (row["context_length"], row["max_output_tokens"]) == limits
        assert row["input_modalities"] == ["text", "image"]
        assert {"tools", "reasoning", "structured_outputs", "prompt_caching"} <= set(row["supported_features"])
        assert row["provider_reliability"]["regions"] == ["eu"]
        assert row["reliability"]["completion_timeout_seconds"] == 900
    assert "private@example.test" not in json.dumps(rows)


@pytest.mark.parametrize("field,value", [
    ("input", "NaN"), ("input", "-1"), ("input", "0.0000001"),
    ("input", 0.19), ("currency", "EUR"), ("cached_input", "0.2"),
    ("minimum_request", "1"), ("cache_write", "0.1"),
])
def test_invalid_prices_fail_closed(field, value):
    payload = catalog()
    payload["data"][0]["pricing"][field] = value
    with pytest.raises(RuntimeError):
        p.discover(payload)


def test_wrong_provider_version_empty_and_duplicate_rejected():
    for change in ("provider", "version", "empty", "duplicate"):
        payload = catalog()
        if change == "provider":
            payload["provider"]["id"] = "other"
        elif change == "version":
            payload["contract_version"] = "1.0"
        elif change == "empty":
            payload["data"] = []
        else:
            payload["data"].append(copy.deepcopy(payload["data"][0]))
        with pytest.raises(RuntimeError):
            p.discover(payload)


def test_future_models_are_discovered_and_retired_models_removed():
    payload = catalog()
    payload["data"][0]["id"] = "negbeta/future"
    payload["data"][1]["lifecycle"]["status"] = "retired"
    prices, rows = p.discover(payload)
    assert set(prices) == set(rows) == {"negbeta/future"}


def test_canary_is_bounded_and_all_routes_remain_held_for_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "MANIFEST_PATH", tmp_path / "negbeta.json")
    monkeypatch.setattr(p, "_DISCOVERED_ROWS", {})
    monkeypatch.setattr(p, "UPSTREAM_ID_MAP", {})
    monkeypatch.setenv("NEGBETA_API_KEY", "test-key")
    calls = []

    def fetch(url, **kwargs):
        assert url == p.URL == f"{p.BASE_URL}/models"
        assert kwargs["extra_headers"] == {"Authorization": "Bearer test-key"}
        return catalog()

    monkeypatch.setattr(p, "fetch_json", fetch)
    monkeypatch.setattr(p, "probe_openai_chat", lambda **kw: calls.append(kw) or kw["model"] == QWEN)
    result = p.fetch()
    assert p._DISCOVERED_ROWS[QWEN]["routable"] is True
    assert p._DISCOVERED_ROWS[DEEPSEEK]["routable_reason"] == "provider-canary-failed"
    assert len(calls) == 2
    for call in calls:
        assert call["max_tokens"] == 64 and call["require_usage"]
        assert call["expected_content"] == "PONG"
        assert call["extra_body"] == {"reasoning_effort": "none"}
    p.write_provider_manifest(result)
    rows = json.loads(p.MANIFEST_PATH.read_text())["models"]
    assert all(row["routable"] is False and row["routable_reason"] == "gateway-rollout-pending" for row in rows)
    p.write_provider_manifest(p.fetch())
    assert len(calls) == 2
    assert all(row["routable"] is False for row in json.loads(p.MANIFEST_PATH.read_text())["models"])
    monkeypatch.delenv("NEGBETA_API_KEY")
    with pytest.raises(RuntimeError, match="required"):
        p.fetch()
    assert not p.UPSTREAM_ID_MAP and not p._DISCOVERED_ROWS
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        p.write_provider_manifest(result)


def test_registration_privacy_and_cloud_secret_wiring():
    from scripts.check_price_coverage import (
        _DISCOVERABLE_MANIFEST_PROVIDERS,
        _STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG,
    )
    from scripts.pricing.refresh import PROVIDER_SLUGS
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.catalog_data import (
        GATEWAY_PREPAID_PROVIDER_SLUGS,
        PRIVACY_TIER_CONFIDENTIAL,
        PRIVACY_TIER_ZERO_RETENTION,
        PROVIDERS,
        ModelEndpoint,
    )
    from trusted_router.catalog_privacy import (
        endpoint_meets_privacy_requirement,
        endpoint_zero_data_retention,
    )
    from trusted_router.provider_locations import PROVIDER_INFERENCE_LOCATIONS
    from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

    provider = PROVIDERS[p.SLUG]
    assert provider.stores_content and not provider.provider_zero_data_retention
    assert not provider.prepaid_zero_data_retention
    assert not provider.provider_confidential_compute and not provider.provider_e2ee
    assert provider.provider_headquarters_country == "GB" and not provider.renewable_energy_inference
    endpoint = ModelEndpoint(id="negbeta-test", model_id=QWEN, provider=p.SLUG, usage_type="Credits")
    assert not endpoint_zero_data_retention(endpoint)
    for privacy in (PRIVACY_TIER_ZERO_RETENTION, PRIVACY_TIER_CONFIDENTIAL):
        assert not endpoint_meets_privacy_requirement(endpoint, privacy)
    assert p.SLUG in GATEWAY_PREPAID_PROVIDER_SLUGS and p.SLUG in PROVIDER_SLUGS
    assert p.SLUG in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert _STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG[p.SLUG] is p
    assert OPENAI_COMPATIBLE_PROVIDERS[p.SLUG] == (("NEGBETA_API_KEY",), p.BASE_URL)
    assert any(item[0] == p.SLUG for item in _DISCOVERABLE_MANIFEST_PROVIDERS)
    assert PROVIDER_INFERENCE_LOCATIONS[p.SLUG].locations == ("European Union",)
    assert not any(ep.provider == p.SLUG for ep in MODEL_ENDPOINTS.values())
    root = Path(__file__).resolve().parents[1]
    assert "NEGBETA_API_KEY:trustedrouter-negbeta-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()
    assert 'ensure_secret_from_env_file "NEGBETA_API_KEY" "trustedrouter-negbeta-api-key"' in (root / "scripts/deploy/secrets.sh").read_text()


def test_eventual_active_endpoint_uses_existing_markup_cache_and_native_identity(monkeypatch, tmp_path):
    from trusted_router import catalog_ingest

    raw = json.loads(p.MANIFEST_PATH.read_text())
    for row in raw["models"]:
        row["routable"] = True
        row.pop("routable_reason", None)
    (tmp_path / "negbeta.json").write_text(json.dumps(raw))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    _, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    for model, context, input_price, output_price, cached in (
        (QWEN, 262144, 200450, 2057250, 47475),
        (DEEPSEEK, 1048576, 316500, 1266000, 31650),
    ):
        ep = endpoints[f"{model}@negbeta/prepaid"]
        assert ep.upstream_id == model and ep.context_length == context
        assert ep.usage_type == "Credits"
        assert ep.prompt_price_microdollars_per_million_tokens == input_price
        assert ep.completion_price_microdollars_per_million_tokens == output_price
        assert ep.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == cached
        assert f"{model}@negbeta/byok" not in endpoints


def test_public_policy_explains_standard_privacy_and_scoped_eu_claim(client):
    response = client.get("/providers/negbeta")
    assert response.status_code == 200
    for text in ("Negbeta", "Standard", "future disk-backed", "180 days", "Cloudflare", "European Union"):
        assert text in response.text
    for url in ("/static/provider-logos/negbeta.png", "/static/og/providers/negbeta.png",
                "https://negbeta.com/", "https://inference.negbeta.com/docs"):
        assert url in response.text
    assert "private@example.test" not in response.text
