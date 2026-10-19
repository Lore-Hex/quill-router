from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.pricing.providers import amazon_bedrock as bedrock

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def tariff(*, native="openai.gpt-oss-20b", suffix="standard", region="us-east-1"):
    result = {"products": {}, "terms": {"OnDemand": {}}}
    for meter, usd in (("input", "0.0000700000"), ("output", "0.0003000000"), ("cache-read", "0.0000070000")):
        result["products"][meter] = {"attributes": {
            "regionCode": region, "usagetype": f"USE1-{native}-mantle-{meter}-tokens-{suffix}",
        }}
        result["terms"]["OnDemand"][meter] = {"term": {
            "effectiveDate": "2026-01-01T00:00:00Z", "priceDimensions": {"rate": {
                "unit": "1K tokens", "beginRange": "0", "endRange": "Inf", "pricePerUnit": {"USD": usd},
            }},
        }}
    return result


def catalog(*models):
    return {"data": [{"id": model, "status": "available"} for model in models]}


def test_exact_usd_per_thousand_conversion_and_cached_price():
    price = bedrock.parse_prices(tariff(), now=NOW)["openai.gpt-oss-20b"]
    assert price.prompt_micro_per_m == 70_000
    assert price.completion_micro_per_m == 300_000
    assert price.tiers[0].prompt_cached_micro_per_m == 7_000


def test_per_million_units_are_not_multiplied_as_per_thousand():
    payload = tariff()
    dim = payload["terms"]["OnDemand"]["input"]["term"]["priceDimensions"]["rate"]
    dim.update(unit="1M tokens", pricePerUnit={"USD": "0.07"})
    assert bedrock.parse_prices(payload, now=NOW)["openai.gpt-oss-20b"].prompt_micro_per_m == 70_000


@pytest.mark.parametrize("suffix", ["global-standard", "priority", "flex", "batch", "standard-batch"])
def test_different_billing_variants_cannot_supply_standard_prices(suffix):
    with pytest.raises(RuntimeError, match="no valid exact"):
        bedrock.parse_prices(tariff(suffix=suffix), now=NOW)


def test_other_region_and_runtime_prices_cannot_be_used():
    with pytest.raises(RuntimeError, match="no valid exact"):
        bedrock.parse_prices(tariff(region="us-west-2"), now=NOW)
    payload = tariff()
    for row in payload["products"].values():
        row["attributes"]["usagetype"] = row["attributes"]["usagetype"].replace("-mantle", "")
    with pytest.raises(RuntimeError, match="no valid exact"):
        bedrock.parse_prices(payload, now=NOW)


@pytest.mark.parametrize("field,value", [
    ("unit", "tokens"), ("beginRange", "1"), ("endRange", "100000"),
    ("pricePerUnit", {"EUR": "0.001"}), ("pricePerUnit", {"USD": "NaN"}),
    ("pricePerUnit", {"USD": "-1"}), ("pricePerUnit", {"USD": "0.00000000001"}),
    ("pricePerUnit", {"USD": True}), ("pricePerUnit", {"USD": 0.00007}),
])
def test_invalid_units_currency_range_and_precision_rejected(field, value):
    payload = tariff()
    payload["terms"]["OnDemand"]["input"]["term"]["priceDimensions"]["rate"][field] = value
    with pytest.raises(RuntimeError):
        bedrock.parse_prices(payload, now=NOW)


def test_current_terms_not_future_discount_and_conflicts_fail_closed():
    payload = tariff()
    terms = payload["terms"]["OnDemand"]["input"]
    future = copy.deepcopy(terms["term"])
    future["effectiveDate"] = "2027-01-01T00:00:00Z"
    future["priceDimensions"]["rate"]["pricePerUnit"]["USD"] = "0.000001"
    terms["future"] = future
    assert bedrock.parse_prices(payload, now=NOW)["openai.gpt-oss-20b"].prompt_micro_per_m == 70_000
    future["effectiveDate"] = terms["term"]["effectiveDate"]
    with pytest.raises(RuntimeError, match="conflicting"):
        bedrock.parse_prices(payload, now=NOW)


def test_duplicate_sku_conflict_and_cache_write_not_silently_ignored():
    payload = tariff()
    payload["products"]["other-input"] = copy.deepcopy(payload["products"]["input"])
    payload["terms"]["OnDemand"]["other-input"] = copy.deepcopy(payload["terms"]["OnDemand"]["input"])
    payload["terms"]["OnDemand"]["other-input"]["term"]["priceDimensions"]["rate"]["pricePerUnit"]["USD"] = "0.00008"
    with pytest.raises(RuntimeError, match="conflicting"):
        bedrock.parse_prices(payload, now=NOW)
    payload = tariff()
    payload["products"]["write"] = {"attributes": {
        "regionCode": "us-east-1", "usagetype": "USE1-openai.gpt-oss-20b-mantle-cache-write-tokens-30m-standard",
    }}
    with pytest.raises(RuntimeError, match="no valid exact"):
        bedrock.parse_prices(payload, now=NOW)


def test_discovery_is_exact_not_fuzzy_and_future_models_are_automatic():
    prices, rows = bedrock.discover(catalog("zai.glm-9", "zai.glm-9-fast"), tariff(native="zai.glm-9"))
    assert set(prices) == {"z-ai/glm-9"}
    assert rows["z-ai/glm-9"]["upstream_id"] == "zai.glm-9"
    assert rows["z-ai/glm-9-fast"]["routable"] is False
    assert rows["z-ai/glm-9-fast"]["routable_reason"] == "price-unavailable"


@pytest.mark.parametrize("payload", [{}, {"data": []}, catalog("invalid"), catalog("openai.gpt-oss-20b", "openai.gpt-oss-20b")])
def test_empty_invalid_duplicate_catalog_does_not_publish(payload):
    with pytest.raises(RuntimeError):
        bedrock.discover(payload, tariff())


@pytest.mark.parametrize("native", sorted(bedrock._AUDIO_CONTRACT_MODELS))
def test_audio_models_cannot_change_existing_transcription_contract(native):
    prices, rows = bedrock.discover(catalog("openai.gpt-oss-20b", native), tariff())
    assert set(prices) == {"openai/gpt-oss-20b"}
    assert rows[bedrock.canonical_model_id(native)]["routable_reason"] == "unsupported-audio-contract"


@pytest.mark.parametrize("region,allowed", [
    ("us-central1", True), ("europe-west4", True),
    ("aws-eu-west-3", False), ("azure-australiaeast", False), ("unknown", False),
])
def test_catalog_waits_for_each_cloud_gateway_upgrade(monkeypatch, region, allowed):
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.catalog_deployment import apply_gateway_admission_holds

    monkeypatch.setenv("TR_ENVIRONMENT", "production")
    monkeypatch.setenv("TR_SERVING_REGION", region)
    models, endpoints = dict(MODELS), dict(MODEL_ENDPOINTS)
    baseline = {key for key, ep in endpoints.items() if ep.provider == "amazon-bedrock"}
    assert baseline
    other_qwen = {key for key, ep in endpoints.items()
                  if ep.model_id == "qwen/qwen3-32b" and ep.provider != "amazon-bedrock"}
    apply_gateway_admission_holds(models, endpoints)
    assert bool(baseline & endpoints.keys()) is allowed
    assert other_qwen <= endpoints.keys()
    if not allowed:
        assert all(ep.provider != "amazon-bedrock" for ep in endpoints.values())


def test_failed_fetch_resets_discovery(monkeypatch):
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    monkeypatch.setattr(bedrock, "_DISCOVERED_ROWS", {"old": {"id": "old"}})
    with pytest.raises(RuntimeError, match="required"):
        bedrock.fetch()
    with pytest.raises(RuntimeError, match="successful discovery"):
        bedrock.write_provider_manifest(None)


def test_canary_requires_usage_and_failed_route_stays_held(monkeypatch, tmp_path):
    monkeypatch.setenv("BEDROCK_API_KEY", "test-token")
    monkeypatch.setattr(bedrock, "MANIFEST_PATH", tmp_path / "bedrock.json")
    monkeypatch.setattr(bedrock, "fetch_json", lambda url, **kw: catalog("openai.gpt-oss-20b") if url == bedrock.URL else tariff())
    monkeypatch.setattr(bedrock, "fetch_model_limits", lambda _model: {})
    def probe(**kwargs):
        assert kwargs["require_usage"] and kwargs["require_message"]
        assert kwargs["base_url"] == bedrock.BASE_URL
        assert kwargs["model"] == "openai.gpt-oss-20b"
        return False
    monkeypatch.setattr(bedrock, "probe_openai_chat", probe)
    result = bedrock.fetch()
    bedrock.write_provider_manifest(result)
    row = json.loads(bedrock.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"


def test_registration_and_conservative_privacy():
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from scripts.pricing.refresh import _import_provider
    from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
    from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

    slug = "amazon-bedrock"
    assert OPENAI_COMPATIBLE_PROVIDERS[slug] == (("BEDROCK_API_KEY",), bedrock.BASE_URL)
    assert slug in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert slug in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert _import_provider("amazon_bedrock") is bedrock
    assert any(row[0] == slug for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
    provider = PROVIDERS[slug]
    assert not provider.supports_byok
    assert not provider.provider_zero_data_retention
    assert not provider.prepaid_zero_data_retention
    assert not provider.provider_e2ee
    root = Path(__file__).resolve().parents[1]
    assert "BEDROCK_API_KEY:trustedrouter-bedrock-api-key" in (root / ".github/workflows/refresh-prices.yml").read_text()


def test_committed_routes_use_bedrock_ids_prices_and_fail_closed_holds():
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.pricing import _customer_price

    endpoint = MODEL_ENDPOINTS["openai/gpt-oss-20b@amazon-bedrock/prepaid"]
    assert endpoint.upstream_id == "openai.gpt-oss-20b"
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(70_000)
    assert endpoint.completion_price_microdollars_per_million_tokens == _customer_price(300_000)
    assert "google/gemma-4-31b-it@amazon-bedrock/prepaid" not in MODEL_ENDPOINTS
    assert "openai/gpt-6-astra@amazon-bedrock/prepaid" not in MODEL_ENDPOINTS


def test_model_card_limits_require_exact_mantle_identity():
    html = '<ul><li>Context window: 128K tokens</li><li>Max output tokens: 16K</li></ul><table><tr><td>bedrock-mantle</td><td>openai.gpt-oss-120b</td></tr></table>'
    assert bedrock.model_card_limits(html, "openai.gpt-oss-120b") == {"context_length": 128_000, "max_output_tokens": 16_000}
    assert bedrock.model_card_limits(html, "openai.gpt-oss-20b") == {}
    assert bedrock.model_card_limits(html.replace("16K", "256K"), "openai.gpt-oss-120b") == {}
    assert bedrock.model_card_limits(html.replace("128K tokens", "unknown"), "openai.gpt-oss-120b") == {}


def test_metadata_failure_does_not_invent_large_limits(monkeypatch):
    def fail(_url):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(bedrock, "fetch_html", fail)
    assert bedrock.fetch_model_limits("openai.gpt-oss-20b") == {}
