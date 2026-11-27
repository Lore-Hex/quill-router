"""NextBit's global and EU contracts must never be conflated."""

import copy
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException

from scripts.pricing.providers import _direct_openai, nextbit, nextbit_eu
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider
from tests.fixture_routes import drop_routes, serve_on_fixture_route
from tests.provider_holds import committed_row, row_taken_down_by_refresh
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
    # This test covers EU isolation, not the operator hold on gemma-3-27b
    # (see test_eu_gemma_3_27b_hold_survives_refresh_and_canary).
    spec = replace(nextbit_eu.CATALOG.spec, operator_hold_reasons={})
    adapter = DirectOpenAIProvider(spec, manifest_path=tmp_path / "eu.json")
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


GEMMA_3_27B = "google/gemma-3-27b-it"
# A NextBit EU route that still probes healthy, so it stays served.
EU_CONTROL = "google/gemma-3-12b-it"


def test_eu_gemma_3_27b_is_held_while_the_provider_rate_limits_it():
    # Since 2026-11-22 every configured-route probe fails with HTTP 429
    # (Sentry QUILL-ROUTER-HT) and the 24h outage hold only deferred it.
    # Only this EU route is held; its global twin and EU siblings serve.
    from tests import catalog_vehicles
    from trusted_router.catalog import MODEL_ENDPOINTS

    assert nextbit_eu._OPERATOR_HOLD_REASONS == {GEMMA_3_27B: "provider-access-unavailable"}
    assert nextbit_eu.CATALOG.spec.operator_hold_reasons == nextbit_eu._OPERATOR_HOLD_REASONS
    assert GEMMA_3_27B not in nextbit.CATALOG.spec.operator_hold_reasons
    rows = {row["id"]: row for row in json.loads(nextbit_eu.MANIFEST_PATH.read_text())["models"]}
    assert rows[GEMMA_3_27B]["upstream_id"] == "gemma3:27b"
    assert rows[GEMMA_3_27B]["routable"] is False
    assert rows[GEMMA_3_27B]["routable_reason"] == "provider-access-unavailable"
    # The control serves, unless the refresh itself holds it as unserved.
    control_held = row_taken_down_by_refresh(rows[EU_CONTROL])
    assert rows[EU_CONTROL].get("routable") is not False or control_held
    built = catalog_vehicles.registry_endpoints()
    assert f"{GEMMA_3_27B}@nextbit-eu/prepaid" not in built
    assert f"{EU_CONTROL}@nextbit-eu/prepaid" in built or control_held
    assert not any(
        endpoint.provider == "nextbit-eu" and endpoint.model_id == GEMMA_3_27B
        for endpoint in MODEL_ENDPOINTS.values()
    )


def test_eu_gemma_3_27b_hold_survives_refresh_and_canary(monkeypatch, tmp_path):
    # Neither a fresh listing with a price nor a passing canary may re-admit
    # the route: only deleting the hold entry does. The shared direct-OpenAI
    # writer keeps the held price for settling already-authorized calls, and
    # the hourly refresh drops the held row from snapshot endpoints.
    from scripts.pricing import refresh

    monkeypatch.setenv("NEXTBIT_API_KEY", "fixture-key")
    manifest = tmp_path / "nextbit-eu.json"
    manifest.write_text(json.dumps({"provider": "nextbit-eu", "models": [
        {"id": GEMMA_3_27B, "upstream_id": "gemma3:27b", "routable": True,
         "input_token_price_per_m": 80_000, "output_token_price_per_m": 350_000},
        {"id": EU_CONTROL, "upstream_id": "gemma3:12b-it", "routable": True,
         "input_token_price_per_m": 80_000, "output_token_price_per_m": 350_000},
    ]}))
    monkeypatch.setattr(
        _direct_openai, "fetch_json",
        lambda *_a, **_kw: {"data": [eu_row(), eu_row("gemma3:12b-it")]},
    )
    probed = []

    def probe(**kwargs):
        probed.append(kwargs["model"])
        return True

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    adapter = DirectOpenAIProvider(nextbit_eu.CATALOG.spec, manifest_path=manifest)
    result = adapter.fetch()
    assert GEMMA_3_27B in result.prices
    # A held route is never canaried, so a PONG cannot lift it.
    assert "gemma3:27b" not in probed
    adapter.write_provider_manifest(result)
    by_id = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert by_id[GEMMA_3_27B]["routable"] is False
    assert by_id[GEMMA_3_27B]["routable_reason"] == "provider-access-unavailable"
    assert by_id[EU_CONTROL]["routable"] is True
    # The hourly refresh keeps the committed held row out of snapshot endpoints.
    assert GEMMA_3_27B in refresh._manifest_held_routes()["nextbit-eu"]


def test_eu_gemma_3_27b_is_out_of_the_committed_snapshot():
    from pathlib import Path

    path = Path(nextbit_eu.MANIFEST_PATH).parents[1] / "openrouter_snapshot.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    model = next(model for model in snapshot["models"] if model["id"] == GEMMA_3_27B)
    providers = {e.get("tr_provider_slug") for e in model["endpoints"]}
    assert "nextbit-eu" not in providers
    # The global route is not held, unless the hourly refresh took Nextbit's
    # own row down this hour.
    global_row = committed_row("nextbit", GEMMA_3_27B)
    assert "nextbit" in providers or (global_row is not None and row_taken_down_by_refresh(global_row))
