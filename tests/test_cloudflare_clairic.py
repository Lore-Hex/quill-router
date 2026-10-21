"""Clairic: native discovery, input-only billing and task documentation."""

import json

import httpx
import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import cloudflare_workers_ai as cf
from tests.pinned_manifests import serve_manifest_rows
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import provider_manifest_price_profile_is_valid
from trusted_router.provider_contracts import (
    provider_model_available_from_gateway_region,
    provider_model_requires_exact_global_settlement,
)
from trusted_router.storage import STORE

PROPERTIES = {"price": [{"unit": "per M input tokens", "price": 0.1, "currency": "USD"}]}
NATIVE_ROW = {
    "id": "opaque-uuid", "name": cf.CLAIRIC_UPSTREAM,
    "task": {"name": "Text Generation"},
    "properties": {**PROPERTIES, "context_window": "65536", "vision": "true"},
}
RESULT = {
    "success": True,
    "result": {"model": "clairic", "truncated": False,
               "answers": {"invoice_number": {"value": "INV-2291"}, "total": {"value": 1080}},
               "usage": {"input_tokens": 308, "output_tokens": 0}},
}


def discover(monkeypatch, tmp_path, *, result=RESULT, funded=True):
    monkeypatch.setenv("CLOUDFLARE_WORKERS_AI_API_TOKEN", "test-key")
    monkeypatch.setenv("CLOUDFLARE_WORKERS_AI_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("TR_CLOUDFLARE_WORKERS_AI_ROUTABLE", "1" if funded else "0")
    monkeypatch.setattr(cf, "MANIFEST_PATH", tmp_path / "cloudflare.json")
    monkeypatch.setattr(cf, "_DISCOVERED_MANIFEST_ROWS", {})
    client = httpx.Client
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-key"
        if request.method == "GET":
            return httpx.Response(200, json={"result": [NATIVE_ROW]})
        assert request.url.path == "/client/v4/accounts/test-account/ai/run/" + cf.CLAIRIC_UPSTREAM
        assert json.loads(request.content) == cf.CLAIRIC_PROBE
        assert request.headers["cf-aig-collect-log-payload"] == "false"
        assert request.headers["cf-aig-skip-cache"] == "true"
        return httpx.Response(200, json=result)

    monkeypatch.setattr(cf.httpx, "Client", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(handle)}))
    pricing = cf.fetch()
    cf.write_provider_manifest(pricing)
    rows = json.loads(cf.MANIFEST_PATH.read_text())["models"]
    return pricing, next(row for row in rows if row["id"] == cf.CLAIRIC_MODEL), calls


def test_input_only_price_is_explicit_not_inferred_for_chat():
    assert cf._model_price(PROPERTIES) is None
    assert cf._model_price(PROPERTIES, input_only=True) == ModelPrice(100_000, 0)
    assert cf._canonical_model_id(cf.CLAIRIC_UPSTREAM) == cf.CLAIRIC_MODEL


@pytest.mark.parametrize("items", [
    [], [{"unit": "per M input tokens", "price": "NaN"}],
    [{"unit": "per M input tokens", "price": "Infinity"}],
    [{"unit": "per M input tokens", "price": 0}],
    [{"unit": "per M input tokens", "price": 0.1, "currency": "EUR"}],
    PROPERTIES["price"] + [{"unit": "per M output tokens", "price": 0.1}],
    PROPERTIES["price"] + [{"unit": "per M cached input tokens", "price": 0.01}],
    PROPERTIES["price"] + PROPERTIES["price"],
    [{"unit": "per 1000 input tokens", "price": 0.1}],
])
def test_changed_or_unknown_billing_contract_is_held(items):
    assert cf._model_price({"price": items}, input_only=True) is None


def test_discovery_native_canary_and_exact_catalog(monkeypatch, tmp_path):
    pricing, row, calls = discover(monkeypatch, tmp_path)
    assert pricing.prices[cf.CLAIRIC_MODEL] == ModelPrice(100_000, 0)
    assert len(calls) == 2
    assert row["routable"] is True
    assert row["context_length"] == 65536
    assert row["upstream_id"] == cf.CLAIRIC_UPSTREAM
    assert row["output_token_price_per_m"] == 0
    assert provider_manifest_price_profile_is_valid(row)
    assert not provider_manifest_price_profile_is_valid({**row, "output_token_price_per_m": 1})
    assert not provider_manifest_price_profile_is_valid({**row, "cached_input_token_price_per_m": 1})
    assert not provider_manifest_price_profile_is_valid({**row, "id": "cloudflare/other"})
    serve_manifest_rows(monkeypatch, tmp_path, cf.SLUG, [row])
    model = MODELS[cf.CLAIRIC_MODEL]
    assert model.context_length == 65536
    assert model.documentation and "one user message" in model.documentation.input_format
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert model.prompt_price_microdollars_per_million_tokens == 105_500
    endpoints = [ep for ep in MODEL_ENDPOINTS.values() if ep.model_id == cf.CLAIRIC_MODEL]
    assert endpoints and all(ep.usage_type == "Credits" and ep.provider == cf.SLUG for ep in endpoints)
    assert all(ep.completion_price_microdollars_per_million_tokens == 0 for ep in endpoints)
    assert provider_model_requires_exact_global_settlement(cf.SLUG, cf.CLAIRIC_MODEL)
    assert not provider_model_requires_exact_global_settlement(cf.SLUG, "cloudflare/other")
    from trusted_router.synthetic.probes import _rotation_prompt, rotation_candidates

    # Availability checks exercise extraction, never a PONG chat. This model
    # produces no tokens and must not enter tokens/second benchmarks.
    assert json.loads(_rotation_prompt(cf.SLUG, cf.CLAIRIC_MODEL))["fields"] == cf.CLAIRIC_PROBE["fields"]
    assert cf.CLAIRIC_MODEL not in rotation_candidates(include_input_only=False).get(cf.SLUG, [])


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": True, "output_tokens": 0},
                                  {"input_tokens": 1.5, "output_tokens": 0},
                                  {"input_tokens": 0, "output_tokens": 0},
                                  {"input_tokens": 308, "output_tokens": 2}])
def test_bad_canary_usage_does_not_publish(monkeypatch, tmp_path, usage):
    result = {**RESULT, "result": {**RESULT["result"], "usage": usage}}
    _, row, _ = discover(monkeypatch, tmp_path, result=result)
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"


def test_unfunded_account_does_not_make_a_paid_probe(monkeypatch, tmp_path):
    _, row, calls = discover(monkeypatch, tmp_path, funded=False)
    assert len(calls) == 1
    assert row["routable"] is False
    assert row["routable_reason"] == "account-unfunded"


@pytest.mark.parametrize("region,allowed", [("us-central1", True), ("europe-west4", True), ("us-east4", True), ("us-west1", True), ("eu-west-1", False), ("uaenorth", False), ("", False)])
def test_older_gateway_fleets_do_not_receive_native_tasks(region, allowed):
    assert provider_model_available_from_gateway_region(cf.SLUG, cf.CLAIRIC_MODEL, region) is allowed


@pytest.mark.parametrize("region", [None, "", "eu-west-1", "eu-west-3", "uaenorth", "australiaeast", "unknown-region"])
def test_authorize_requires_an_upgraded_explicit_gateway(client, monkeypatch, tmp_path, region):
    _, row, _ = discover(monkeypatch, tmp_path)
    serve_manifest_rows(monkeypatch, tmp_path, cf.SLUG, [row])
    key = client.post("/v1/keys", headers={"x-trustedrouter-user": "clairic-region@example.com"}, json={"name": "Clairic region"})
    assert key.status_code == 201
    payload = {
        "api_key_hash": key.json()["data"]["hash"], "model": cf.CLAIRIC_MODEL,
        "estimated_input_tokens": 10, "max_output_tokens": 64,
    }
    if region is not None:
        payload["region"] = region
    response = client.post("/v1/internal/gateway/authorize", json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "provider_not_supported"


def test_settles_multi_pass_meter_exactly_once(client, monkeypatch, tmp_path):
    _, row, _ = discover(monkeypatch, tmp_path)
    serve_manifest_rows(monkeypatch, tmp_path, cf.SLUG, [row])
    key = client.post("/v1/keys", headers={"x-trustedrouter-user": "clairic-test@example.com"}, json={"name": "Clairic billing"})
    assert key.status_code == 201
    auth = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key.json()["data"]["hash"], "model": cf.CLAIRIC_MODEL,
        "estimated_input_tokens": 10, "max_output_tokens": 64, "region": "us-central1",
    })
    assert auth.status_code == 200, auth.text
    assert auth.json()["data"]["provider"] == cf.SLUG
    payload = {"authorization_id": auth.json()["data"]["authorization_id"],
               "actual_input_tokens": 308, "actual_output_tokens": 0,
               "request_id": "clairic-billing-test", "elapsed_seconds": 0.2}
    first = client.post("/v1/internal/gateway/settle", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["cost_microdollars"] == token_cost_microdollars(308, 105_500)
    generation = STORE.get_generation(first.json()["data"]["generation_id"])
    assert generation.tokens_prompt == 308 and generation.tokens_completion == 0
    second = client.post("/v1/internal/gateway/settle", json=payload)
    assert second.status_code == 200 and second.json()["data"]["already_settled"]
    assert second.json()["data"]["generation_id"] == generation.id
