"""System One models must never be sent to the chat-completions adapter.

Reviewed Baseten System One models route only as hosted decisions
(POST /v1/decide -> the gateway's System One adapter); every other
decision-models/ ID stays held.
"""
import json

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import baseten
from tests.lifecycle_clock import CATALOG_CLOCK
from tests.test_decide_models import _authorize
from trusted_router import catalog_ingest
from trusted_router.provider_contracts import (
    BASETEN_SYSTEMONE_GATEWAY_REGIONS,
    BASETEN_SYSTEMONE_MODEL_IDS,
    provider_model_available_from_gateway_region,
    provider_model_operator_held,
)
from trusted_router.provider_manifest_policy import decision_manifest_price_is_valid

DECISIONS = (
    "decision-models/baseten-elephant-9b-latest",
    "decision-models/baseten-mammoth-27b-latest",
    "decision-models/baseten-joey-2b-latest",
)
MAMMOTH = "decision-models/baseten-mammoth-27b-latest"


def test_reviewed_set_is_exactly_the_published_models():
    assert frozenset(DECISIONS) == BASETEN_SYSTEMONE_MODEL_IDS


@pytest.mark.parametrize("native", (*DECISIONS, "decision-models/future-model"))
def test_unreviewed_or_renamed_systemone_ids_stay_held(native):
    assert provider_model_operator_held("baseten", "decision-models/future-model", native)
    assert provider_model_operator_held("baseten", "alternate/canonical-name", native)
    assert provider_model_operator_held("baseten", native, "decision-models/other-native")
    assert not provider_model_operator_held("another-provider", native)
    assert not provider_model_operator_held("baseten", "moonshotai/kimi-k3")


@pytest.mark.parametrize("model", DECISIONS)
def test_reviewed_ids_are_admitted_only_under_their_exact_native_id(model):
    assert not provider_model_operator_held("baseten", model, model)


def _feed():
    rows = [{"id": model, "pricing": {"prompt": "0.0000001", "completion": "0"}} for model in DECISIONS]
    rows.append({"id": "decision-models/future-model", "pricing": {"prompt": "0.0000001", "completion": "0"}})
    rows.append({"id": "moonshotai/Kimi-K3", "pricing": {"prompt": "0.000001", "completion": "0.000003"}})
    return {"data": rows}


@pytest.fixture
def isolated_baseten(monkeypatch, tmp_path):
    monkeypatch.setattr(baseten, "EXPECTED_MODELS", [])
    monkeypatch.setattr(baseten, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(baseten, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(baseten, "MANIFEST_PATH", tmp_path / "baseten.json")
    return tmp_path / "baseten.json"


@pytest.mark.parametrize("healthy", [False, True])
def test_discovery_publishes_decide_routes_only_after_systemone_canary(
    monkeypatch, isolated_baseten, httpx_mock, healthy,
):
    monkeypatch.setenv("BASETEN_API_KEY", "test-token")
    probed: list[str] = []

    def probe(key, upstream):
        assert key == "test-token"
        probed.append(upstream)
        return healthy

    monkeypatch.setattr(baseten, "probe_systemone", probe)
    httpx_mock.add_response(url=baseten.URL, json=_feed(), is_reusable=True)
    for _ in range(2):
        result = baseten.fetch()
        baseten.write_provider_manifest(result)
        saved = {row["id"]: row for row in json.loads(isolated_baseten.read_text())["models"]}
        for model in DECISIONS:
            row = saved[model]
            assert row["model_type"] == "decision"
            assert row["endpoints"] == ["decide"]
            assert row["input_modalities"] == ["text"]
            assert row["output_modalities"] == ["decision"]
            assert row["input_token_price_per_m"] == 100_000
            assert row["output_token_price_per_m"] == 0
            assert "cached_input_token_price_per_m" not in row
            assert row["routable"] is healthy
            if healthy:
                assert "routable_reason" not in row
                assert decision_manifest_price_is_valid(row)
            else:
                assert row["routable_reason"] == "provider-canary-failed"
        future = saved["decision-models/future-model"]
        assert future["model_type"] == "decision" and future["endpoints"] == ["decide"]
        assert future["routable"] is False
        assert future["routable_reason"] == "systemone-adapter-not-implemented"
        assert saved["moonshotai/kimi-k3"].get("routable", True) is True
    # Unreviewed IDs are never sent a paid canary.
    assert sorted(set(probed)) == sorted(DECISIONS)


def test_missing_key_fails_decision_canary_closed(monkeypatch, isolated_baseten, httpx_mock):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.setattr(baseten, "probe_systemone", lambda *_: pytest.fail("probed without a key"))
    httpx_mock.add_response(url=baseten.URL, json=_feed())
    baseten.write_provider_manifest(baseten.fetch())
    saved = {row["id"]: row for row in json.loads(isolated_baseten.read_text())["models"]}
    assert all(saved[model]["routable"] is False for model in DECISIONS)


@pytest.mark.parametrize("pricing", [
    {"prompt": "0.0000001", "completion": "0.0000001"},
    {"prompt": "0", "completion": "0"},
])
def test_non_input_only_tariff_is_held(monkeypatch, isolated_baseten, httpx_mock, pricing):
    monkeypatch.setenv("BASETEN_API_KEY", "test-token")
    monkeypatch.setattr(baseten, "probe_systemone", lambda *_: True)
    rows = [{"id": MAMMOTH, "pricing": pricing},
            {"id": "moonshotai/Kimi-K3", "pricing": {"prompt": "0.000001", "completion": "0.000003"}}]
    httpx_mock.add_response(url=baseten.URL, json={"data": rows})
    baseten.write_provider_manifest(baseten.fetch())
    saved = {row["id"]: row for row in json.loads(isolated_baseten.read_text())["models"]}
    assert saved[MAMMOTH]["routable"] is False
    assert saved[MAMMOTH]["model_type"] == "decision"


@pytest.mark.parametrize("change", [None, "status", "model", "answer", "input", "output", "missing", "bool"])
def test_systemone_canary_checks_identity_answer_and_usage(httpx_mock, change):
    body = {
        "model": MAMMOTH,
        "answers": {"color": {"type": "choice", "choice": "red", "probabilities": {"red": 0.99, "blue": 0.01}}},
        "usage": {"input_tokens": 62, "output_tokens": 1},
    }
    status = 200
    if change == "status":
        status = 404
    elif change == "model":
        body["model"] = "decision-models/baseten-joey-2b-latest"
    elif change == "answer":
        body["answers"]["color"]["choice"] = "blue"
    elif change == "input":
        body["usage"]["input_tokens"] = 0
    elif change == "output":
        body["usage"]["output_tokens"] = -1
    elif change == "missing":
        del body["usage"]
    elif change == "bool":
        body["usage"]["input_tokens"] = True
    httpx_mock.add_response(url=baseten.SYSTEMONE_URL, json=body, status_code=status)
    assert baseten.probe_systemone("test-token", MAMMOTH) is (change is None)
    sent = httpx_mock.get_request()
    assert sent.headers["Authorization"] == "Bearer test-token"
    payload = json.loads(sent.content)
    assert payload["model"] == MAMMOTH
    assert payload["questions"]["color"]["type"] == "choice"


def _manifest_row(model=MAMMOTH, **changes):
    row = {
        "id": model, "upstream_id": model, "display_name": "mammoth-1", "title": model,
        "model_type": "decision", "endpoints": ["decide"], "status": 1, "context_length": 16384,
        "input_modalities": ["text"], "output_modalities": ["decision"],
        "supported_features": [], "supported_sampling_parameters": [],
        "routable": True, "input_token_price_per_m": 100_000, "output_token_price_per_m": 0,
    }
    row.update(changes)
    return row


def _write_manifest(tmp_path, rows):
    (tmp_path / "baseten.json").write_text(json.dumps({
        "provider": "baseten", "generated_at": CATALOG_CLOCK.isoformat(),
        "price_scale": "microdollars_per_million", "models": rows,
    }))


@pytest.mark.parametrize("changes", [
    {"model_type": "chat", "endpoints": ["chat/completions"], "output_token_price_per_m": 100_000},
    {"model_type": "chat", "endpoints": ["chat/completions"]},
    {"endpoints": ["chat/completions", "decide"]},
    {"upstream_id": "decision-models/other-native"},
    {"output_token_price_per_m": 1},
    {"cached_input_token_price_per_m": 0},
])
def test_stale_or_altered_manifest_never_creates_chat_or_unpriced_routes(monkeypatch, tmp_path, changes):
    _write_manifest(tmp_path, [_manifest_row(**changes)])
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    model = models.get(MAMMOTH)
    assert model is None or not model.supports_chat
    assert not [e for e in endpoints.values() if e.model_id == MAMMOTH]


def test_unreviewed_decision_row_is_not_served(monkeypatch, tmp_path):
    _write_manifest(tmp_path, [_manifest_row("decision-models/future-model")])
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert "decision-models/future-model" not in models
    assert not endpoints


@pytest.fixture
def served_mammoth(monkeypatch, tmp_path):
    from tests.fixture_routes import bypass_catalog_caches
    from trusted_router import catalog

    _write_manifest(tmp_path, [_manifest_row()])
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    model = models[MAMMOTH]
    monkeypatch.setitem(catalog.MODELS, model.id, model)
    for endpoint in endpoints.values():
        monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, endpoint)
    bypass_catalog_caches(monkeypatch)
    return model, endpoints


def test_mammoth_is_a_decision_only_input_billed_route(served_mammoth):
    model, endpoints = served_mammoth
    assert model.supports_decide and not model.supports_chat
    assert model.output_modalities == ("decision",)
    assert model.input_modalities == ("text",)
    assert model.context_length == 16384
    assert model.prompt_price_microdollars_per_million_tokens > 100_000
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert {(e.provider, e.upstream_id) for e in endpoints.values()} == {("baseten", MAMMOTH)}
    assert "Credits" in {e.usage_type for e in endpoints.values()}


@pytest.mark.asyncio
@pytest.mark.parametrize("region", sorted(BASETEN_SYSTEMONE_GATEWAY_REGIONS))
async def test_decide_authorizes_baseten_systemone_and_chat_is_rejected(served_mammoth, region):
    request = {"model": MAMMOTH, "route_type": "decide", "estimated_input_tokens": 140, "max_output_tokens": 1, "region": region}
    response = await _authorize(request)
    assert response.status_code == 200, response.text
    routes = response.json()["data"]["route_candidates"]
    assert routes and {(row["provider"], row["upstream_model"]) for row in routes} == {("baseten", MAMMOTH)}
    response = await _authorize({**request, "route_type": "chat"})
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("region", [None, "", "eu-west-1", "eu-west-3", "uaenorth", "australiaeast", "unknown-region"])
async def test_decide_requires_a_gateway_with_the_systemone_adapter(served_mammoth, region):
    request = {"model": MAMMOTH, "route_type": "decide", "estimated_input_tokens": 140, "max_output_tokens": 1}
    if region is not None:
        request["region"] = region
    response = await _authorize(request)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "provider_not_supported"


def test_gateway_admission_is_explicit_per_model():
    for model in DECISIONS:
        for region in ["", "unknown-region", "uaenorth", "eu-west-1", *BASETEN_SYSTEMONE_GATEWAY_REGIONS]:
            assert provider_model_available_from_gateway_region("baseten", model, region) == (
                region in BASETEN_SYSTEMONE_GATEWAY_REGIONS
            )
    assert provider_model_available_from_gateway_region("baseten", "moonshotai/kimi-k3", "uaenorth")


def test_committed_catalog_has_no_baseten_systemone_chat_routes():
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS

    for endpoint in MODEL_ENDPOINTS.values():
        if endpoint.provider == "baseten" and endpoint.upstream_id.startswith("decision-models/"):
            assert endpoint.upstream_id in BASETEN_SYSTEMONE_MODEL_IDS
            assert MODELS[endpoint.model_id].supports_decide
            assert not MODELS[endpoint.model_id].supports_chat
    rows = {row["id"]: row for row in json.loads(baseten.MANIFEST_PATH.read_text())["models"]}
    for model in DECISIONS:
        assert rows[model]["model_type"] == "decision"
        assert rows[model]["endpoints"] == ["decide"]


def test_classification_drops_cache_tariff_from_decision_prices():
    discovered = {MAMMOTH: {"id": MAMMOTH, "upstream_id": MAMMOTH, "display_name": "mammoth-1"}}
    prices = {MAMMOTH: ModelPrice(100_000, 0, prompt_cached_micro_per_m=0)}
    checked, healthy = baseten.classify_systemone_rows(prices, discovered, None)
    assert checked == {MAMMOTH} and healthy == set()
    assert prices[MAMMOTH].tiers[0].prompt_cached_micro_per_m is None
