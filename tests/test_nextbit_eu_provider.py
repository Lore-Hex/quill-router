"""NextBit's global and EU contracts must never be conflated."""

import copy
import json

import pytest
from fastapi import HTTPException

from scripts.pricing.providers import _direct_openai, nextbit, nextbit_eu
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider
from tests.fixture_routes import drop_routes, serve_on_fixture_route
from trusted_router.catalog import PROVIDERS
from trusted_router.catalog_data import EU_FOCUSED_PROVIDER_ORDER
from trusted_router.catalog_privacy import (
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    provider_privacy_tier,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates


def eu_row(native="gemma3:27b"):
    return {
        "id": native,
        "context_length": 32768,
        "pricing": {"prompt": "0.00000008", "completion": "0.00000035", "input_cache_read": "0.00000004"},
        "datacenters": [{"country_code": "ES"}],
    }


def test_eu_discovery_prices_canaries_and_manifest_are_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXTBIT_API_KEY", "fixture-key")
    calls = []

    def fetch(url, *, extra_headers):
        assert url == "https://eu.api.nextbit256.com/v1/models"
        assert extra_headers == {"Authorization": "Bearer fixture-key"}
        return {"data": [eu_row(), eu_row("gemma3:12b-it")]}

    def probe(**kwargs):
        calls.append(kwargs)
        return kwargs["model"] == "gemma3:27b"

    monkeypatch.setattr(_direct_openai, "fetch_json", fetch)
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    adapter = DirectOpenAIProvider(nextbit_eu.CATALOG.spec, manifest_path=tmp_path / "eu.json")
    result = adapter.fetch()
    assert set(result.prices) == {"google/gemma-3-27b-it", "google/gemma-3-12b-it"}
    price = result.prices["google/gemma-3-27b-it"]
    assert price.prompt_micro_per_m == 80_000
    assert price.completion_micro_per_m == 350_000
    for call in calls:
        assert call["base_url"] == nextbit_eu.BASE_URL != nextbit.BASE_URL
        assert call["require_usage"] and call["require_message"]
        assert call["expected_content"] == "PONG"
    assert {call["model"] for call in calls} == {"gemma3:27b", "gemma3:12b-it"}
    adapter.write_provider_manifest(result)
    payload = json.loads(adapter.manifest_path.read_text())
    rows = {row["id"]: row for row in payload["models"]}
    healthy = rows["google/gemma-3-27b-it"]
    assert healthy["upstream_id"] == "gemma3:27b"
    assert healthy["cached_input_token_price_per_m"] == 40_000
    assert healthy["routable"] is True
    assert rows["google/gemma-3-12b-it"]["routable"] is False
    assert not any("deepseek" in model for model in rows)


@pytest.mark.parametrize("datacenters", [None, [], {}, [{}], [{"country_code": "US"}], [{"country_code": "ES"}, {"country_code": "US"}]])
def test_eu_discovery_rejects_missing_or_changed_residency(monkeypatch, tmp_path, datacenters):
    row = eu_row()
    row["datacenters"] = datacenters
    monkeypatch.setenv("NEXTBIT_API_KEY", "fixture-key")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *a, **kw: {"data": [row]})
    adapter = DirectOpenAIProvider(nextbit_eu.CATALOG.spec, manifest_path=tmp_path / "eu.json")
    with pytest.raises(RuntimeError, match="Spain-only"):
        adapter.fetch()
    assert not adapter.manifest_path.exists()


def test_eu_discovery_outage_does_not_read_global(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXTBIT_API_KEY", "fixture-key")
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise RuntimeError("EU unavailable")

    monkeypatch.setattr(_direct_openai, "fetch_json", fetch)
    adapter = DirectOpenAIProvider(nextbit_eu.CATALOG.spec, manifest_path=tmp_path / "eu.json")
    with pytest.raises(RuntimeError, match="EU unavailable"):
        adapter.fetch()
    assert calls == [nextbit_eu.URL]


def test_eu_does_not_change_global_privacy():
    assert provider_privacy_tier(PROVIDERS["nextbit"]) == PRIVACY_TIER_STANDARD
    eu = PROVIDERS["nextbit-eu"]
    assert provider_privacy_tier(eu) == PRIVACY_TIER_ZERO_RETENTION
    assert not eu.stores_content
    assert not eu.provider_e2ee and not eu.provider_confidential_compute
    assert not eu.supports_byok
    assert "nextbit-eu" in EU_FOCUSED_PROVIDER_ORDER
    assert "nextbit" not in EU_FOCUSED_PROVIDER_ORDER


def test_eu_privacy_filter_and_explicit_pin_do_not_leak_to_global(monkeypatch):
    model_id = "qwen/qwen3-14b"
    drop_routes(monkeypatch, model_id)
    for provider in ("nextbit", "nextbit-eu"):
        serve_on_fixture_route(monkeypatch, model_id, provider, author="qwen", upstream_id="qwen3:14b")
    body = {"model": model_id, "provider": {"usage": "credits", "min_privacy": "zdr"}}
    settings = Settings(environment="test")
    assert [ep.provider for _, ep in chat_route_endpoint_candidates(body, settings)] == ["nextbit-eu"]
    pinned = copy.deepcopy(body)
    pinned["provider"].update({"only": ["nextbit-eu"], "allow_fallbacks": False})
    assert [ep.provider for _, ep in chat_route_endpoint_candidates(pinned, settings)] == ["nextbit-eu"]
    pinned["provider"]["min_privacy"] = "confidential"
    with pytest.raises(HTTPException) as confidential_error:
        chat_route_endpoint_candidates(pinned, settings)
    assert confidential_error.value.status_code == 400
    drop_routes(monkeypatch, model_id)
    serve_on_fixture_route(monkeypatch, model_id, "nextbit", author="qwen", upstream_id="qwen3:14b")
    pinned["provider"]["min_privacy"] = "zdr"
    with pytest.raises(HTTPException) as missing_eu_error:
        chat_route_endpoint_candidates(pinned, settings)
    assert missing_eu_error.value.status_code == 400


@pytest.mark.parametrize("alias", ["trustedrouter/zdr", "trustedrouter/eu"])
def test_eu_routes_are_eligible_for_constrained_aliases(monkeypatch, alias):
    model_id = "qwen/qwen3-14b"
    drop_routes(monkeypatch, model_id)
    for provider in ("nextbit", "nextbit-eu"):
        serve_on_fixture_route(monkeypatch, model_id, provider, author="qwen", upstream_id="qwen3:14b")
    routes = chat_route_endpoint_candidates(
        {"model": alias, "provider": {"only": ["nextbit-eu"], "allow_fallbacks": False}},
        Settings(environment="test"),
    )
    assert routes
    assert {endpoint.provider for _, endpoint in routes} == {"nextbit-eu"}
