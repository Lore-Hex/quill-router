from __future__ import annotations

from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from tests.fixture_routes import bypass_catalog_caches
from trusted_router import dashboard
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS
from trusted_router.catalog_data import PRIVACY_TIER_ZERO_RETENTION, Model, ModelEndpoint
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement
from trusted_router.provider_locations import provider_geography
from trusted_router.provider_precision import endpoint_precision_metadata

MODEL_ID = "deepseek/deepseek-v4.1-flash-1017"


def route(provider: str, upstream: str) -> ModelEndpoint:
    return ModelEndpoint(id=f"{MODEL_ID}@{provider}/prepaid", model_id=MODEL_ID,
                         provider=provider, usage_type="Credits", upstream_id=upstream)


@pytest.mark.parametrize(("provider", "upstream", "evidence"), [
    ("tinfoil", "deepseek-v4-1-flash", "published_serving_config"),
    ("sail-research", "deepseek-ai/DeepSeek-V4.1-Flash-1017", "provider_declaration"),
    ("deepinfra", "deepseek-ai/DeepSeek-V4.1-Flash-1017", "provider_declaration"),
    ("featherless", "deepseek-ai/DeepSeek-V4.1-Flash-1017", "provider_declaration"),
    ("novita", "deepseek/deepseek-v4.1-flash-1017", "provider_declaration"),
])
def test_1017_precision_is_exact_route_evidence_not_inferred(provider, upstream, evidence):
    endpoint = route(provider, upstream)
    precision = endpoint_precision_metadata(endpoint)
    assert precision and precision["evidence_type"] == evidence
    assert precision["reviewed_on"] == "2026-10-28"
    assert precision["runtime_verified"] is False
    assert precision["quantization"] == "fp8"
    assert precision["sources"]
    if provider == "tinfoil":
        assert precision["model_revision"] == "1bae0e1088993b9b82de08f59de5be5a34e42170"
        assert precision["kv_cache_dtype"] == "fp8_ds_mla"
        assert precision["weight_formats"] == ["fp8", "fp4"]
    else:
        assert precision["model_revision"] is None
    assert endpoint_precision_metadata(replace(endpoint, upstream_id=upstream + "-other")) is None


@pytest.mark.parametrize("provider", ["sail-research", "featherless"])
def test_newly_verified_policies_qualify_for_zdr_only(provider):
    endpoint = route(provider, "deepseek-ai/DeepSeek-V4.1-Flash-1017")
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
    assert PROVIDERS[provider].stores_content is False
    assert PROVIDERS[provider].provider_confidential_compute is not True
    assert PROVIDERS[provider].provider_e2ee is not True


@pytest.mark.parametrize("provider", ["engy", "makora", "cloudflare-workers-ai", "boundless"])
def test_conditional_or_downstream_unverified_policies_stay_out_of_strict_zdr(provider):
    assert not endpoint_meets_privacy_requirement(route(provider, MODEL_ID), PRIVACY_TIER_ZERO_RETENTION)


def test_geography_separates_offices_dynamic_placement_and_optional_pinning():
    nebius = provider_geography("nebius")
    assert nebius["headquarters"]["location"] == "Amsterdam, Netherlands"
    assert nebius["inference"]["locations"] == ("Global (dynamic public endpoints)",)
    sail = provider_geography("sail-research")
    assert sail["operator_country"] is None
    assert "US-only" in sail["inference"]["provider_pinning"] or "[US]" in sail["inference"]["provider_pinning"]
    assert "does not enforce" in sail["inference"]["declaration"]
    assert provider_geography("boundless")["inference"]["locations"] == (
        "United States", "United Kingdom", "European Union (countries unspecified)",
    )
    assert "international processing is permitted" in provider_geography("makora")["inference"]["declaration"]


def test_model_table_exposes_sources_location_and_real_timing_only(monkeypatch, test_settings):
    monkeypatch.setitem(MODELS, MODEL_ID, Model(id=MODEL_ID, name="DeepSeek 1017", provider="deepseek", context_length=1_048_576))
    for provider, upstream in [("sail-research", "deepseek-ai/DeepSeek-V4.1-Flash-1017"),
                               ("novita", MODEL_ID), ("engy", "deepseek-v4.1-flash-1017")]:
        endpoint = route(provider, upstream)
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
    bypass_catalog_caches(monkeypatch)
    monkeypatch.setattr(dashboard, "measured_for_model", lambda model_id, **kw: [
        {"provider": "novita", "model": MODEL_ID, "p50_ttft_ms": 1234,
         "p50_tokens_per_second": 97.25, "sample_count": 10,
         "ttft_sample_count": 4, "throughput_sample_count": 4,
         "last_seen": "2026-10-28T18:00:00Z"},
        {"provider": "sail-research", "model": MODEL_ID, "p50_ttft_ms": 10,
         "p50_tokens_per_second": 999, "sample_count": 100,
         "ttft_sample_count": 1, "throughput_sample_count": 0},
    ])
    html = dashboard.public_model_detail_html(test_settings, MODEL_ID)
    soup = BeautifulSoup(html, "html.parser")
    novita = soup.select_one("#provider-novita")
    assert "TTFT: 1.23 s" in novita.get_text()
    assert "97.2 tokens/s" in novita.get_text()
    assert novita.select_one("time")["datetime"] == "2026-10-28T18:00:00Z"
    assert "Operator country: US" in novita.get_text()
    assert "processing countries unconfirmed, no fixed-region commitment" in novita.get_text()
    assert novita.select_one('a[href="https://novita.ai/legal/privacy-policy"]')
    assert novita.select_one('[data-label="Measured speed"]')
    assert novita.select_one('[data-label="Company / inference location"]')
    assert soup.select_one('[aria-label="Provider comparison"]')["tabindex"] == "0"
    sail = soup.select_one("#provider-sail-research")
    assert "Operator country: Not verified" in sail.get_text()
    assert "TTFT: insufficient data" in sail.get_text()
    assert "Speed: insufficient data" in sail.get_text()
    assert "999" not in sail.get_text()
    engy = soup.select_one("#provider-engy")
    assert "provider.zdr=true" in engy.get_text()
    assert "TTFT: insufficient data" in engy.get_text()
    assert "Unknown" in engy.get_text()
    assert "TR router attested" not in [th.get_text() for th in soup.select(".model-route-table th")]
