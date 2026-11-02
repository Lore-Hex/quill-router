import json
from decimal import Decimal

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import regolo
from tests.lifecycle_clock import CATALOG_CLOCK
from tests.test_decide_models import _authorize
from trusted_router import catalog_ingest
from trusted_router.provider_manifest_policy import decision_manifest_price_is_valid


def decision_row(**changes):
    return {
        "model_group": "clef-flash", "mode": "decision",
        "max_input_tokens": 65536.0, "supports_vision": True,
        "input_cost_per_token": "0.00000015", "output_cost_per_token": 0,
        **changes,
    }


def manifest_row():
    prices, rows = regolo.discover_decisions({"data": [decision_row()]}, Decimal("1.1551"))
    row = rows[regolo.DECISION_MODEL_ID]
    return {
        **row, "routable": True,
        "input_token_price_per_m": prices[regolo.DECISION_MODEL_ID].prompt_micro_per_m,
        "output_token_price_per_m": 0,
    }


def test_decision_discovery_does_not_invent_chat_image_or_output_billing():
    row = manifest_row()
    assert row["input_token_price_per_m"] == 173265
    assert row["output_token_price_per_m"] == 0
    assert row["endpoints"] == ["decide"]
    assert row["input_modalities"] == ["text"]
    assert row["supported_features"] == []
    assert decision_manifest_price_is_valid(row)
    assert regolo.normalize_catalog({"data": [decision_row()]}, Decimal("1.1")) == []
    assert regolo.CATALOG.model_id("clef-flash") == "cloudflare/clef-flash"


@pytest.mark.parametrize("changes", [
    {"mode": "chat"}, {"max_input_tokens": None},
    {"input_cost_per_token": "NaN"}, {"input_cost_per_token": 0},
    {"input_cost_per_token": -1}, {"input_cost_per_token": True},
    {"output_cost_per_token": None}, {"output_cost_per_token": 0.000001},
    {"output_cost_per_token": -1}, {"output_cost_per_token": "Infinity"},
])
def test_changed_decision_contract_fails_closed(changes):
    with pytest.raises(RuntimeError):
        regolo.discover_decisions({"data": [decision_row(**changes)]}, Decimal("1.1"))


def test_unknown_decisions_and_duplicate_names_are_not_enabled():
    assert regolo.discover_decisions({"data": [decision_row(model_group="unknown")]}, Decimal(1)) == ({}, {})
    with pytest.raises(RuntimeError, match="duplicate"):
        regolo.discover_decisions({"data": [decision_row(), decision_row()]}, Decimal(1))


@pytest.mark.parametrize("change", [None, "model", "input", "output", "missing", "bool"])
def test_live_canary_checks_identity_and_reported_usage(httpx_mock, change):
    body = {"model": "clef-flash", "answers": {"color": {"choice": "red"}}, "usage": {"input_tokens": 62, "output_tokens": 0}}
    if change == "model":
        body["model"] = "different-model"
    elif change == "input":
        body["usage"]["input_tokens"] = 0
    elif change == "output":
        body["usage"]["output_tokens"] = 1
    elif change == "missing":
        del body["usage"]
    elif change == "bool":
        body["usage"]["output_tokens"] = False
    httpx_mock.add_response(url="https://api.regolo.ai/v1/systemone", json=body)
    assert regolo.probe_decision("test-token") is (change is None)
    sent = httpx_mock.get_request()
    assert sent.headers["Authorization"] == "Bearer test-token"
    assert json.loads(sent.content)["model"] == "clef-flash"


@pytest.mark.parametrize("healthy", [False, True])
def test_hourly_refresh_preserves_decision_protocol_and_canary(monkeypatch, tmp_path, healthy):
    path = tmp_path / "regolo.json"
    result = ProviderPricingResult(slug="regolo", prices={"qwen/qwen3.5-9b": ModelPrice(100000, 200000)}, source="api", fetched_url=regolo.URL)
    monkeypatch.setattr(regolo.CATALOG, "manifest_path", path)
    monkeypatch.setattr(regolo.CATALOG, "discovered_rows", {"qwen/qwen3.5-9b": {"id": "qwen/qwen3.5-9b", "upstream_id": "qwen3.5-9b"}})
    monkeypatch.setattr(regolo.CATALOG, "_fetched", True)
    monkeypatch.setattr(regolo.CATALOG, "fetch", lambda: result)
    monkeypatch.setattr(regolo, "fetch_json", lambda *a, **k: {"data": [decision_row()]})
    monkeypatch.setattr(regolo, "fetch_html", lambda _: "unused")
    monkeypatch.setattr(regolo, "usd_per_eur", lambda _: Decimal("1.1551"))
    monkeypatch.setattr(regolo, "probe_decision", lambda _: healthy)
    monkeypatch.setenv("REGOLO_API_KEY", "test-token")
    regolo.write_provider_manifest(regolo.fetch())
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    row = rows[regolo.DECISION_MODEL_ID]
    assert row["routable"] is healthy
    assert row["model_type"] == "decision" and row["endpoints"] == ["decide"]
    assert row["input_token_price_per_m"] == 173265 and row["output_token_price_per_m"] == 0
    assert "qwen/qwen3.5-9b" in rows


@pytest.fixture
def served_clef(monkeypatch, tmp_path):
    from tests.fixture_routes import bypass_catalog_caches
    from trusted_router import catalog

    path = tmp_path / "regolo.json"
    path.write_text(json.dumps({"provider": "regolo", "generated_at": CATALOG_CLOCK.isoformat(), "price_scale": "microdollars_per_million", "models": [manifest_row()]}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    model = models[regolo.DECISION_MODEL_ID]
    monkeypatch.setitem(catalog.MODELS, model.id, model)
    for endpoint in endpoints.values():
        monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, endpoint)
    bypass_catalog_caches(monkeypatch)
    return model, endpoints


def test_regolo_decision_catalog_routes_and_markup(served_clef):
    model, endpoints = served_clef
    assert model.supports_decide and not model.supports_chat
    assert model.output_modalities == ("decision",)
    assert model.context_length == 65536
    assert model.prompt_price_microdollars_per_million_tokens == 182795
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert {(e.provider, e.upstream_id) for e in endpoints.values()} == {("regolo", "clef-flash")}


def test_generic_chat_listing_cannot_inherit_clef_decision_protocol(monkeypatch, tmp_path):
    from trusted_router import catalog_ingest

    row = {
        **manifest_row(), "model_type": "chat", "endpoints": ["chat/completions"],
        "upstream_id": "cloudflare/clef-flash", "output_token_price_per_m": 100_000,
    }
    path = tmp_path / "makora.json"
    path.write_text(json.dumps({
        "provider": "makora", "generated_at": CATALOG_CLOCK.isoformat(),
        "price_scale": "microdollars_per_million", "models": [row],
    }))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert regolo.DECISION_MODEL_ID not in models
    assert not endpoints


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["us-central1", "us-east4", "us-west1", "europe-west4"])
async def test_decide_authorizes_regolo_and_chat_is_rejected(served_clef, region):
    request = {"model": regolo.DECISION_MODEL_ID, "route_type": "decide", "estimated_input_tokens": 140, "max_output_tokens": 1, "region": region}
    response = await _authorize(request)
    assert response.status_code == 200, response.text
    routes = response.json()["data"]["route_candidates"]
    assert routes and {(row["provider"], row["upstream_model"]) for row in routes} == {("regolo", "clef-flash")}
    response = await _authorize({**request, "route_type": "chat"})
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("region", [None, "", "eu-west-1", "eu-west-3", "uaenorth", "australiaeast", "unknown-region"])
async def test_decision_requires_a_configured_explicit_gateway(served_clef, region):
    request = {"model": regolo.DECISION_MODEL_ID, "route_type": "decide", "estimated_input_tokens": 140, "max_output_tokens": 1}
    if region is not None:
        request["region"] = region
    response = await _authorize(request)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "provider_not_supported"


def test_snapshot_preserves_decision_gateway_admission(served_clef):
    from trusted_router.provider_contracts import provider_model_available_from_gateway_region
    from trusted_router.routing_snapshot import _gateway_regions

    _, endpoints = served_clef
    for endpoint in endpoints.values():
        rule = _gateway_regions(endpoint)
        assert rule.requires_explicit_origin
        assert rule.only == ("europe-west4", "us-central1", "us-east4", "us-west1")
        assert not rule.deny
        for region in ["", "unknown-region", "uaenorth", "australiaeast", "eu-west-1", "eu-west-3", *rule.only]:
            assert provider_model_available_from_gateway_region(endpoint.provider, endpoint.model_id, region) == (region in rule.only)
    assert provider_model_available_from_gateway_region("regolo", "qwen/qwen3.5-9b", "uaenorth")


@pytest.mark.catalog_as_built
def test_catalog_overrides_stale_chat_classification():
    from trusted_router.catalog import MODELS

    model = MODELS[regolo.DECISION_MODEL_ID]
    assert model.supports_decide and not model.supports_chat
    assert model.input_modalities == ("text",)
