from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.catalog import (
    MODELS,
    PROVIDERS,
    endpoints_for_model,
    model_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.config import Settings
from trusted_router.custom_model_rules import require_custom_model_base_model
from trusted_router.internal_model_access import require_internal_model_key
from trusted_router.pricing import _customer_price
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.routing_candidates import (
    auto_candidate_models,
    cheap_candidate_models,
    fast_candidate_models,
    monitor_candidate_models,
)
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE
from trusted_router.synthetic.probes import rotation_candidates

MODEL = "trustedrouter/fortanix-test"
KEY = "sk-tr-internal-fortanix-fixture"


def test_reference_tariff_native_id_and_conservative_privacy() -> None:
    model = MODELS[MODEL]
    assert model.internal_only and model.context_length == 131_072
    assert not model.byok_available
    assert model_to_openrouter_shape(model)["trustedrouter"]["internal_only"]
    provider = PROVIDERS["fortanix"]
    assert provider.internal_only and not provider.supports_byok
    assert not provider.provider_zero_data_retention
    assert not provider.provider_confidential_compute
    assert not provider.provider_e2ee
    routes = endpoints_for_model(MODEL)
    assert len(routes) == 1
    route = routes[0]
    assert route.provider == "fortanix" and route.upstream_id == "GLM-5.3-Flash"
    assert route.prompt_price_microdollars_per_million_tokens == _customer_price(150_000)
    assert route.completion_price_microdollars_per_million_tokens == _customer_price(500_000)
    assert route.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens is None
    assert not route.is_byok


@pytest.mark.parametrize("body", [
    {"model": MODEL}, {"model": "nyte/fortanix-test"},
    {"models": ["openai/gpt-4.1-mini", f" {MODEL} "]},
    {"models": ["nyte/fortanix-test"]},
])
@pytest.mark.parametrize("configured", [False, True])
def test_internal_key_is_mandatory_even_for_aliases_and_fallback_arrays(body, configured) -> None:
    settings = Settings(environment="test", synthetic_monitor_api_key=KEY if configured else "")
    for key in (None, "", lookup_hash_api_key("customer")):
        with pytest.raises(HTTPException) as error:
            require_internal_model_key(body, key, settings)
        assert error.value.status_code == 403
    if configured:
        require_internal_model_key(body, lookup_hash_api_key(KEY), settings)
    else:
        with pytest.raises(HTTPException):
            require_internal_model_key(body, lookup_hash_api_key(KEY), settings)


def test_only_explicit_internal_route_can_select_fortanix() -> None:
    settings = Settings(environment="test")
    routes = chat_route_endpoint_candidates({"model": MODEL}, settings)
    assert [(m.id, e.provider) for m, e in routes] == [(MODEL, "fortanix")]
    for pool in (auto_candidate_models, cheap_candidate_models, fast_candidate_models, monitor_candidate_models):
        assert all(model.id != MODEL for model in pool())
    assert all(provider.slug != "fortanix" for provider in providers_for_display())
    assert "fortanix" not in rotation_candidates()
    with pytest.raises(HTTPException):
        require_custom_model_base_model(MODEL)


@pytest.mark.parametrize("provider", [{"zdr": True}, {"min_privacy": "confidential"}, {"usage": "byok"}])
def test_private_test_does_not_satisfy_byok_or_privacy_filters(provider) -> None:
    with pytest.raises(HTTPException):
        chat_route_endpoint_candidates({"model": MODEL, "provider": provider}, Settings(environment="test"))


@pytest.mark.parametrize("region", ["azure-australiaeast", "uaenorth", "unknown-region"])
def test_non_allowlisted_cloud_is_explicitly_disabled(region: str) -> None:
    settings = Settings(environment="test", serving_region=region, synthetic_monitor_api_key=KEY)
    with pytest.raises(HTTPException) as error:
        require_internal_model_key({"model": MODEL}, lookup_hash_api_key(KEY), settings)
    assert error.value.status_code == 503


@pytest.mark.parametrize("path", [
    "/v1/models", "/v1/models/picker", "/v1/providers", "/models", "/providers",
    "/sitemap.xml", "/llms.txt", "/llms-full.txt",
    "/v1/models/trustedrouter/fortanix-test/endpoints",
])
def test_internal_test_is_absent_from_public_surfaces(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "fortanix" not in response.text.lower()
    assert "armet.ai" not in response.text.lower()


@pytest.mark.parametrize("path", [
    "/providers/fortanix", "/providers/fortanix/performance",
    "/models/trustedrouter/fortanix-test", "/models/trustedrouter/fortanix-test/pricing",
])
def test_guessed_private_pages_do_not_publish_a_listing(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
def test_customer_cannot_invoke_internal_model(client, inference_headers, path) -> None:
    response = client.post(path, headers=inference_headers, json={
        "model": MODEL, "input": "PONG", "messages": [{"role": "user", "content": "PONG"}],
        "max_tokens": 256,
    })
    assert response.status_code == 403, response.text


def test_private_gateway_admission_and_bill_once(client, user_headers, test_settings, monkeypatch) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "internal fixture"}).json()
    workspace_id = created["data"]["workspace_id"]
    payload = {
        "api_key_hash": created["data"]["hash"], "model": MODEL,
        "estimated_input_tokens": 18, "max_output_tokens": 256,
    }
    denied = client.post("/v1/internal/gateway/authorize", json=payload)
    assert denied.status_code == 403, denied.text
    assert STORE.credit_money[workspace_id].reserved_microdollars == 0
    monkeypatch.setattr(test_settings, "stage_d_probe_api_key", created["key"])
    authorized = client.post("/v1/internal/gateway/authorize", json=payload)
    assert authorized.status_code == 200, authorized.text
    auth = authorized.json()["data"]
    assert auth["provider"] == "fortanix"
    assert auth["upstream_model"] == "GLM-5.3-Flash"
    assert auth["usage_type"] == "Credits"
    assert STORE.credit_money[workspace_id].reserved_microdollars > 0
    settle_body = {
        "authorization_id": auth["authorization_id"],
        "actual_input_tokens": 18, "actual_output_tokens": 92,
        "request_id": "fortanix-private-fixture", "finish_reason": "stop",
    }
    settled = client.post("/v1/internal/gateway/settle", json=settle_body)
    assert settled.status_code == 200, settled.text
    money = STORE.credit_money[workspace_id]
    assert money.reserved_microdollars == 0
    assert money.total_usage_microdollars > 0
    charged = money.total_usage_microdollars
    repeat = client.post("/v1/internal/gateway/settle", json=settle_body)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["data"]["already_settled"]
    assert STORE.credit_money[workspace_id].total_usage_microdollars == charged


@pytest.mark.parametrize("balanced", [True, False])
def test_internal_benchmarks_stay_private(monkeypatch, balanced: bool) -> None:
    from tests.test_leaderboard import _sample
    from trusted_router import benchmark_samples

    private = _sample(provider="fortanix", model=MODEL)
    public = _sample(provider="openai", model="openai/gpt-4.1-mini")
    readers = {"provider_benchmark_samples": lambda **kwargs: [private, public]}
    if balanced:
        readers["provider_balanced_benchmark_samples"] = lambda **kwargs: [private, public]
    monkeypatch.setattr(benchmark_samples, "STORE", SimpleNamespace(**readers))
    assert benchmark_samples.public_benchmark_samples(limit=10) == [public]
