from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import cohere, voyage
from tests.rerank_manifest_fixtures import (
    MANIFEST_STATES,
    install_public_rerank_models,
    provider_manifest,
)
from tests.test_rerank_billing import authorize, error, seed
from trusted_router import catalog_ingest
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.catalog_data import PROVIDERS, Model
from trusted_router.main import create_app
from trusted_router.pricing import _flat_tier

CONTEXTS = {
    "rerank-3": 32_000, "rerank-3-lite": 32_000,
    "rerank-2.5": 32_000, "rerank-2.5-lite": 32_000,
    "rerank-2": 16_000, "rerank-2-lite": 8_000,
    "rerank-1": 8_000, "rerank-lite-1": 4_000,
}
COHERE_IDS = {
    "cohere/rerank-5-fast": "rerank-v5.0-fast",
    "cohere/rerank-5-pro": "rerank-v5.0-pro",
}
WIRE_PATH = Path(__file__).parent / "fixtures/gateway_repository_rerank_wire_golden.json"


def test_rerank_ids_and_contexts(monkeypatch, tmp_path):
    install_public_rerank_models(monkeypatch, tmp_path)
    rows = json.loads((tmp_path / "voyage.json").read_text())["models"]
    rows = {row["upstream_id"]: row for row in rows if row.get("model_type") == "rerank"}
    assert set(rows) == set(CONTEXTS)
    for upstream_id, context in CONTEXTS.items():
        row = rows[upstream_id]
        assert row["id"] == "voyage/" + upstream_id
        assert row["context_length"] == context
        assert row.get("routable") is not False
        model = MODELS[row["id"]]
        assert model.supports_rerank and model.context_length == context
    assert not PROVIDERS["cohere"].supports_rerank
    rows = json.loads((tmp_path / "cohere.json").read_text())["models"]
    assert {row["id"]: row["upstream_id"] for row in rows
            if row.get("model_type") == "rerank"} == COHERE_IDS


def test_voyage_origin_public_output(client, monkeypatch):
    from types import SimpleNamespace

    from tests.fixture_routes import clear_catalog_caches
    from trusted_router import catalog_data, middleware, model_regions
    from trusted_router.workspace_origin_policy import eligible_candidates

    monkeypatch.setattr(middleware, "secrets", SimpleNamespace(token_urlsafe=lambda _: "fixed-test-nonce"))
    embeddings = {model.id for model in MODELS.values()
                  if model.id.startswith("voyage/") and model.supports_embeddings}
    assert embeddings
    paths = ["/v1/models", "/v1/models/count", "/v1/embeddings/models", "/models",
             "/v1/models?provider[jurisdiction]=us"]
    paths += [f"/models/{model_id}" for model_id in sorted(embeddings)]
    pairs = [(MODELS[endpoint.model_id], endpoint) for endpoint in MODEL_ENDPOINTS.values()
             if endpoint.model_id in embeddings]

    def observe():
        clear_catalog_caches()
        pages = {}
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, path
            pages[path] = response.content
        return pages, model_regions.model_region_evidence("us-ai-models"), {
            policy: [endpoint.id for _, endpoint in eligible_candidates(policy, pairs)]
            for policy in ("unrestricted", "us", "western")
        }

    with monkeypatch.context() as patch:
        patch.delitem(catalog_data.MODEL_ORIGINS, "voyage")
        before_pages, before_region, before_policy = observe()
    after_pages, after_region, after_policy = observe()
    assert after_pages == before_pages
    voyage_lab = next(row for row in after_region["labs"] if row["lab_name"] == "Voyage AI")
    public_voyage = {model.id for model in MODELS.values()
                     if model.provider == "voyage" and (model.supports_embeddings or model.supports_rerank)}
    assert {row["id"] for row in voyage_lab["models"]} == {
        m.id for m in sorted((MODELS[key] for key in public_voyage), key=lambda m: (-sum(e.model_id == m.id for e in MODEL_ENDPOINTS.values()), m.id))[:model_regions.MODELS_PER_LAB]
    }
    assert voyage_lab["model_count"] == len(public_voyage)
    assert voyage_lab["hidden_model_count"] == max(0, len(public_voyage) - model_regions.MODELS_PER_LAB)
    assert after_region["origin_model_count"] == before_region["origin_model_count"] + len(public_voyage)
    assert after_region["lab_count"] == before_region["lab_count"] + 1
    assert after_region["models_without_recorded_origin"] == before_region["models_without_recorded_origin"] - len(public_voyage)
    assert after_region["directory_model_count"] == before_region["directory_model_count"]
    assert before_policy["unrestricted"] == after_policy["unrestricted"]
    assert before_policy["us"] == before_policy["western"] == []
    # The current operators are outside the workspace policy's direct-operator list.
    assert after_policy == before_policy
    print(json.dumps({
        "embeddings": sorted(embeddings),
        "unchanged_pages": paths,
        "region_before": {key: before_region[key] for key in (
            "origin_model_count", "lab_count", "models_without_recorded_origin", "directory_model_count")},
        "region_after": {key: after_region[key] for key in (
            "origin_model_count", "lab_count", "models_without_recorded_origin", "directory_model_count")},
        "origin_policy_before": before_policy, "origin_policy_after": after_policy,
    }, sort_keys=True))


@pytest.mark.parametrize("state", MANIFEST_STATES)
@pytest.mark.parametrize("provider", [cohere, voyage], ids=lambda p: p.SLUG)
def test_refresh_preserves_api_ids_contexts_and_explicit_holds(state, provider, tmp_path, monkeypatch):
    initial = provider_manifest(provider.SLUG, state)
    if state != "without-rerank":
        initial["models"][1].update(routable=False, routable_reason="review-pending")
    path = tmp_path / f"{provider.SLUG}.json"
    path.write_text(json.dumps(initial))
    monkeypatch.setattr(provider, "MANIFEST_PATH", path)
    rows = provider_manifest(provider.SLUG, "post-refresh")["models"]
    result = ProviderPricingResult(provider.SLUG, {
        row["id"]: ModelPrice(row["input_token_price_per_m"], 0) for row in rows
    }, "deterministic", fetched_url=provider.URL)
    unknown = f"{provider.SLUG}/rerank-future"
    result.prices[unknown] = ModelPrice(90_000, 0)
    for _ in range(2):
        notes = provider.write_provider_manifest(result)
        written = {row["id"]: row for row in json.loads(path.read_text())["models"]}
        if provider is cohere:
            assert unknown not in written
            assert any(unknown in note and "no documented API id" in note for note in notes)
            for model_id, api_id in COHERE_IDS.items():
                assert written[model_id]["upstream_id"] == api_id
        else:
            assert written[unknown]["routable"] is False
            assert written[unknown]["routable_reason"] == "api-model-unconfirmed"
            assert any(unknown in note for note in notes)
            for api_id, context in CONTEXTS.items():
                assert written["voyage/" + api_id]["context_length"] == context
        if state != "without-rerank":
            held = written[initial["models"][1]["id"]]
            assert held["routable"] is False and held["routable_reason"] == "review-pending"
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setitem(PROVIDERS, provider.SLUG, replace(PROVIDERS[provider.SLUG], supports_rerank=True))
    assert unknown not in catalog_ingest._rerank_models()


@pytest.mark.parametrize("fixture", ["cohere.html", "cohere_pricing_2026-11-24.html"])
def test_cohere_fetch_skips_unknown_display_label(fixture, monkeypatch):
    from scripts.pricing import base

    page = (Path(__file__).parent / "fixtures/pricing" / fixture).read_text()
    page += '{"modelName":"Rerank Future","per":"1M tokens","pricings":[{"inputLabel":"Cost","inputPrice":0.05}]}'
    monkeypatch.setattr(base, "fetch_html", lambda *_a, **_kw: page)
    monkeypatch.setattr(cohere, "fetch_html", lambda *_a, **_kw: page)
    result = cohere.fetch()
    assert "cohere/rerank-future" not in result.prices
    assert any("cohere/rerank-future" in note and "no documented API id" in note for note in result.notes)


@pytest.mark.parametrize("model_id", ["anthropic/claude-haiku-4.5", "openai/text-embedding-3-large"])
def test_checked_in_provider_enforces_rerank_route(model_id, monkeypatch):
    from fastapi import HTTPException

    from trusted_router import gateway_timing

    assert PROVIDERS["voyage"].supports_rerank
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 0.0)
    _, _, key = seed()
    with pytest.raises(HTTPException) as caught:
        authorize(key, model_id, "rerank")
    assert caught.value.status_code == 400
    assert caught.value.detail == error(f"Model does not support rerank: {model_id}")


def test_gateway_repository_rerank_wire_authorize_and_settle(monkeypatch, test_settings):
    raw = WIRE_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "99a87690bec5cfc34f29e3885ac173f1a7c3d767fb754267f4d3d6c3e4f9e1a7"
    wire = json.loads(raw)
    body = json.loads(wire[0]["body"])
    store, workspace, key = seed()
    del store.api_keys.key_ids_by_lookup_hash[key.lookup_hash]
    key.lookup_hash = body["api_key_lookup_hash"]
    store.api_keys.key_ids_by_lookup_hash[key.lookup_hash] = key.hash
    assert store.get_key_by_lookup_hash(body["api_key_lookup_hash"]) is key
    price = 1_000_000
    model = Model(
        id=body["model"], name="Wire fixture reranker", provider="voyage",
        context_length=32_000, upstream_id=body["model"].split("/", 1)[1],
        supports_chat=False, supports_rerank=True, prepaid_available=True, byok_available=False,
        prompt_price_microdollars_per_million_tokens=price,
        published_prompt_price_microdollars_per_million_tokens=price,
        completion_price_microdollars_per_million_tokens=0,
        published_completion_price_microdollars_per_million_tokens=0,
        price_tiers=_flat_tier(price, 0), published_price_tiers=_flat_tier(price, 0),
    )
    assert PROVIDERS["voyage"].supports_rerank
    monkeypatch.setitem(MODELS, model.id, model)
    endpoint = next(iter(catalog_ingest._build_endpoints({model.id: model}).values()))
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
    settings = test_settings.model_copy(update={
        "internal_gateway_token": wire[0]["headers"]["X-Trustedrouter-Internal-Token"][0],
    })
    client = TestClient(create_app(settings, init_observability=False, configure_store_arg=False))
    headers = {key: value[0] for key, value in wire[0]["headers"].items()}
    response = client.post(wire[0]["path"], content=wire[0]["body"], headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["workspace_id"] == workspace.id
    assert data.get("settlement_mode", "sync") == "sync"
    assert not data.get("async_eligible", False)
    settle = json.loads(wire[1]["body"])
    settle["authorization_id"] = data["authorization_id"]
    settle["selected_endpoint"] = data["endpoint_id"]
    response = client.post(wire[1]["path"], json=settle,
                           headers={key: value[0] for key, value in wire[1]["headers"].items()})
    assert response.status_code == 200, response.text
    expected = price * settle["actual_input_tokens"] // 1_000_000
    assert response.json()["data"]["cost_microdollars"] == expected
    assert store.credit_money[workspace.id].total_usage_microdollars == expected
