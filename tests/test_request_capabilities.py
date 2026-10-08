from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixture_routes import drop_routes, serve_on_fixture_route
from trusted_router import catalog, catalog_ingest, request_capabilities
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL, Model, ModelEndpoint
from trusted_router.request_capabilities import (
    EFFORT_ORDER,
    endpoint_capabilities,
    model_capabilities,
    normalize_request_capabilities,
)
from trusted_router.routing import RoutePreferences, catalog_endpoint_candidates


def _model(monkeypatch, model_id, provider, *, modalities=("text",)):
    drop_routes(monkeypatch, model_id)
    model = Model(
        id=model_id, name=model_id, provider=provider, context_length=8192,
        input_modalities=modalities,
    )
    monkeypatch.setitem(catalog.MODELS, model_id, model)
    return model


def _route(monkeypatch, model, provider, **kwargs):
    endpoint = serve_on_fixture_route(
        monkeypatch, model.id, provider, author=model.provider, **kwargs,
    )
    model, endpoints = normalize_request_capabilities(catalog.MODELS[model.id], [endpoint])
    monkeypatch.setitem(catalog.MODELS, model.id, model)
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, endpoints[0])
    return endpoints[0]


@pytest.mark.parametrize(("model_id", "provider", "efforts", "vision"), [
    ("openai/gpt-5", "openai", ["minimal", "low", "medium", "high"], True),
    ("openai/gpt-5.2", "openai", ["none", "low", "medium", "high", "xhigh"], True),
    ("openai/gpt-5.5", "openai", ["none", "low", "medium", "high", "xhigh"], True),
    ("openai/gpt-6-astra", "openai", ["low", "medium", "high", "xhigh", "max"], True),
    ("anthropic/claude-sonnet-4.6", "anthropic", ["none", "low", "medium", "high", "max"], True),
    ("anthropic/claude-haiku-4.5", "anthropic", ["none", "minimal", "low", "medium", "high", "xhigh"], True),
    ("mistralai/mistral-large", "mistral", [], False),
])
def test_public_models_exact_capabilities(client, monkeypatch, model_id, provider, efforts, vision):
    model = _model(monkeypatch, model_id, provider)
    _route(monkeypatch, model, provider, input_modalities=("text", "image") if vision else ("text",))
    response = client.get("/v1/models")
    assert response.status_code == 200
    rows = [row for row in response.json()["data"] if row["id"] == model_id]
    assert len(rows) == 1
    expected = dict(reasoning_effort=efforts, tools=True, seed=False, vision=vision, confidential=False)
    assert rows[0]["trustedrouter"]["capabilities"] == expected
    endpoints = rows[0]["trustedrouter"]["endpoints"]
    assert len(endpoints) == 1
    assert endpoints[0]["capabilities"] == expected
    detail = client.get(f"/v1/models/{model_id}/endpoints")
    assert detail.status_code == 200
    assert len(detail.json()["data"]) == 1
    assert detail.json()["data"][0]["trustedrouter"]["capabilities"] == expected


@pytest.mark.catalog_as_built
def test_catalog_models_publish_verified_unions_and_unknown_effort(client):
    response = client.get("/v1/models")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    expected = {
        "openai/gpt-5.5": dict(reasoning_effort=["none", "low", "medium", "high", "xhigh"], tools=True, seed=True, vision=True, confidential=False),
        # Native Mistral discovery declares vision for both generations;
        # Large 4's reasoning support does not imply known effort values.
        "mistralai/mistral-large": dict(reasoning_effort=[], tools=True, seed=False, vision=True, confidential=False),
        "mistralai/mistral-large-4": dict(reasoning_effort=None, tools=True, seed=False, vision=True, confidential=False),
        # The reviewed native V4 Flash contract must survive the union. Its
        # separately cataloged 0731 variant has no reviewed effort contract.
        "deepseek/deepseek-v4-flash": dict(reasoning_effort=["high", "max"], tools=True, seed=True, vision=False, confidential=False),
        "deepseek/deepseek-v4-flash-0731": dict(reasoning_effort=None, tools=True, seed=True, vision=False, confidential=True),
        "openai/gpt-oss-120b": dict(reasoning_effort=["low", "medium", "high"], tools=True, seed=True, vision=False, confidential=True),
        "google/gemini-3-flash-preview": dict(reasoning_effort=None, tools=True, seed=True, vision=True, confidential=False),
    }
    for model_id, capabilities in expected.items():
        assert rows[model_id]["trustedrouter"]["capabilities"] == capabilities, model_id
    # The recovered, reviewed Chutes TEE route supplies this capability;
    # an unqualified provider badge must not be its source.
    flash_routes = rows["deepseek/deepseek-v4-flash-0731"]["trustedrouter"]["endpoints"]
    assert {route["provider"] for route in flash_routes if route["capabilities"]["confidential"]} == {"chutes"}
    assert "capabilities" not in rows["trustedrouter/auto"]["trustedrouter"]


@pytest.mark.catalog_as_built
def test_whole_catalog_capabilities_agree_with_legacy_declarations(client):
    response = client.get("/v1/models")
    assert response.status_code == 200
    rows = response.json()["data"]
    concrete = [row for row in rows if "capabilities" in row["trustedrouter"]]
    assert len(concrete) > 500
    # Each listed route is looked up among the effective endpoints, which is
    # what the listing is built from.
    effective = {
        endpoint.id: endpoint
        for model_id in catalog.MODELS
        for endpoint in catalog.endpoints_for_model(model_id)
    }
    for row in concrete:
        capabilities = row["trustedrouter"]["capabilities"]
        assert capabilities["tools"] == ("tools" in row["supported_parameters"]), row["id"]
        assert capabilities["seed"] == ("seed" in row["supported_parameters"]), row["id"]
        assert capabilities["vision"] == ("image" in row["architecture"]["input_modalities"]), row["id"]
        for route in row["trustedrouter"]["endpoints"]:
            endpoint = effective[route["id"]]
            flags = route["capabilities"]
            for parameter in ("tools", "seed"):
                assert flags[parameter] == (parameter in endpoint.supported_parameters), endpoint.id
                assert flags[parameter] == (parameter in route["supported_parameters"]), endpoint.id
            assert flags["vision"] == ("image" in endpoint.input_modalities), endpoint.id
            if flags["reasoning_effort"] is not None:
                assert bool(flags["reasoning_effort"]) == ("reasoning_effort" in endpoint.supported_parameters), endpoint.id


@pytest.mark.catalog_as_built
def test_gpt_55_verified_tools_keep_openai_with_require_parameters(client):
    model = catalog.MODELS["openai/gpt-5.5"]
    prefs = RoutePreferences(require_parameters=True, requested_parameters=frozenset({"tools"}))
    routes = catalog_endpoint_candidates(model, prefs)
    assert {endpoint.id for _, endpoint in routes if endpoint.provider == "openai"} == {
        "openai/gpt-5.5@openai/prepaid", "openai/gpt-5.5@openai/byok",
    }
    response = client.get(f"/v1/models/{model.id}/endpoints")
    assert response.status_code == 200
    openai = [row for row in response.json()["data"] if row["provider_name"] == "OpenAI"]
    assert len(openai) == 2
    for row in openai:
        assert "tools" in row["supported_parameters"]
        assert row["trustedrouter"]["capabilities"]["tools"] is True
        assert row["trustedrouter"]["capabilities"]["seed"] is False


@pytest.mark.catalog_as_built
def test_reviewed_tools_contracts_publish_and_keep_provider_routes(client):
    response = client.get("/v1/models")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    prefs = RoutePreferences(require_parameters=True, requested_parameters=frozenset({"tools"}))

    # Positive and negative controls keep documentation evidence provider-scoped.
    for provider, model_id, tools in (
        ("siliconflow", "z-ai/glm-5.3", True),
        ("cloudflare-workers-ai", "openai/gpt-oss-120b", True),
        ("kimi", "moonshotai/kimi-k3", True),
        ("google-vertex", "google/gemini-3.5-flash-lite", True),
        ("siliconflow", "openai/gpt-oss-120b", False),
        ("cloudflare-workers-ai", "qwen/qwq-32b", False),
    ):
        endpoints = [
            endpoint for endpoint in rows[model_id]["trustedrouter"]["endpoints"]
            if endpoint["provider"] == provider
        ]
        assert endpoints, (provider, model_id)
        for endpoint in endpoints:
            assert endpoint["capabilities"]["tools"] is tools, endpoint["id"]
            assert ("tools" in endpoint["supported_parameters"]) is tools, endpoint["id"]
            if provider == "kimi":
                assert endpoint["capabilities"]["reasoning_effort"] == ["low", "high", "max"]

    data = json.loads(request_capabilities._CONTRACT_PATH.read_text())
    pairs = {
        (provider, model_id)
        for contract in data["contracts"] if contract.get("tools") is True
        for provider in contract["providers"] for model_id in contract["models"]
    }
    present = set()
    for provider, model_id in sorted(pairs):
        row = rows.get(model_id)
        endpoints = [
            endpoint for endpoint in row["trustedrouter"]["endpoints"]
            if endpoint["provider"] == provider
        ] if row else []
        # A scheduled cutover or the hourly refresh can retire a reviewed
        # provider/model pair, so no presence floor here: one would turn main
        # red on routine catalog churn. The pinned controls above keep this
        # test from passing vacuously.
        if not endpoints:
            continue
        present.add((provider, model_id))
        candidates = {
            endpoint.id for _, endpoint in catalog_endpoint_candidates(catalog.MODELS[model_id], prefs)
        }
        for endpoint in endpoints:
            assert endpoint["capabilities"]["tools"] is True, endpoint["id"]
            assert "tools" in endpoint["supported_parameters"], endpoint["id"]
            assert endpoint["id"] in candidates, endpoint["id"]
    assert pairs
    assert present


def test_tools_only_contract_preserves_unknown_effort_and_declared_parameter(client, monkeypatch):
    model = _model(monkeypatch, "fixture/tools-only", "novita")
    contract = {
        "providers": ["novita"], "models": [model.id],
        "source": "Fixture provider's own tools documentation", "tools": True,
    }
    _assert_reviewed_contracts([contract])
    monkeypatch.setattr(
        request_capabilities, "_reviewed_contracts", lambda: {("novita", model.id): contract},
    )
    endpoint = _route(monkeypatch, model, "novita", supported_parameters=("reasoning_effort",))
    assert endpoint_capabilities(model, endpoint)["reasoning_effort"] is None
    assert set(endpoint.supported_parameters) == {"tools", "reasoning_effort"}
    prefs = RoutePreferences(
        require_parameters=True, requested_parameters=frozenset({"tools", "reasoning_effort"}),
    )
    assert [route.id for _, route in catalog_endpoint_candidates(model, prefs)] == [endpoint.id]

    response = client.get("/v1/models")
    assert response.status_code == 200
    row = next(row for row in response.json()["data"] if row["id"] == model.id)
    [published] = row["trustedrouter"]["endpoints"]
    assert published["capabilities"]["reasoning_effort"] is None
    assert "reasoning_effort" in published["supported_parameters"]
    response = client.get(f"/v1/models/{model.id}/endpoints")
    assert response.status_code == 200
    [published] = response.json()["data"]
    assert published["trustedrouter"]["capabilities"]["reasoning_effort"] is None
    assert "reasoning_effort" in published["supported_parameters"]


def test_mistral_large_rejected_effort_excludes_route_with_require_parameters(monkeypatch):
    model = _model(monkeypatch, "mistralai/mistral-large", "mistral")
    endpoint = _route(monkeypatch, model, "mistral", supported_parameters=("reasoning_effort", "seed"))
    prefs = RoutePreferences(requested_parameters=frozenset({"reasoning_effort"}))
    assert [route.id for _, route in catalog_endpoint_candidates(model, prefs)] == [endpoint.id]
    assert catalog_endpoint_candidates(model, replace(prefs, require_parameters=True)) == []
    assert endpoint.supported_parameters == ("tools",)
    assert endpoint_capabilities(model, endpoint)["reasoning_effort"] == []


@pytest.mark.parametrize("supported", [True, False, None])
def test_reviewed_declarations_are_applied_once_at_construction(monkeypatch, supported):
    model = Model(
        id="fixture/reviewed", name="Fixture", provider="novita", context_length=8192,
        supported_parameters=("tools", "seed", "reasoning_effort"), input_modalities=("text", "image"),
    )
    review = {} if supported is None else {
        "tools": supported, "seed": supported, "vision": supported,
        "reasoning_effort": ["low"] if supported else [],
    }
    monkeypatch.setattr(request_capabilities, "_reviewed_contracts", lambda: {("novita", model.id): review})
    parameters = () if supported else ("tools", "seed", "reasoning_effort")
    endpoint = ModelEndpoint(
        id="fixture", model_id=model.id, provider="novita", usage_type="Credits",
        supported_parameters=parameters, input_modalities=("text",) if supported else ("text", "image"),
    )
    corrected_model, [corrected] = normalize_request_capabilities(model, [endpoint])
    expected = () if supported is False else ("tools", "reasoning_effort", "seed")
    assert corrected.supported_parameters == expected
    assert corrected.input_modalities == (("text",) if supported is False else ("text", "image"))
    assert corrected_model.supported_parameters == (() if supported is False else model.supported_parameters)
    assert corrected_model.input_modalities == corrected.input_modalities
    # Serialization consumes the corrected declarations, even if the source
    # contracts are no longer available. It must not apply a second override.
    monkeypatch.setattr(request_capabilities, "_reviewed_contracts", lambda: {})
    assert endpoint_capabilities(corrected_model, corrected) == dict(
        reasoning_effort=None, tools=supported is not False, seed=supported is not False,
        vision=supported is not False, confidential=False,
    )


@pytest.mark.parametrize("model_id", ["openai/gpt-oss-120b", "openai/gpt-oss-20b"])
def test_gpt_oss_correction_removes_images_from_models_and_routes(model_id):
    model = Model(id=model_id, name="Fixture", provider="openai", context_length=8192, input_modalities=("image", "text"))
    endpoint = ModelEndpoint(id="fixture", model_id=model_id, provider="novita", usage_type="Credits", input_modalities=("text", "image"))
    model, [endpoint] = normalize_request_capabilities(model, [endpoint])
    assert model.input_modalities == ("text",)
    assert endpoint.input_modalities == ("text",)
    assert endpoint_capabilities(model, endpoint)["vision"] is False


def test_model_declarations_survive_routes_with_unknown_support(monkeypatch):
    model = _model(monkeypatch, "fixture/model-declarations", "novita", modalities=("text", "image"))
    model = replace(model, supported_parameters=("tools", "seed"))
    monkeypatch.setitem(catalog.MODELS, model.id, model)
    endpoint = _route(monkeypatch, model, "novita", input_modalities=("text",))
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model.id])
    assert row["supported_parameters"] == ["tools", "max_tokens", "seed"]
    assert row["architecture"]["input_modalities"] == ["text", "image"]
    assert row["trustedrouter"]["capabilities"] == dict(
        reasoning_effort=None, tools=True, seed=True, vision=True, confidential=False,
    )
    assert endpoint_capabilities(model, endpoint) == dict(
        reasoning_effort=None, tools=False, seed=False, vision=False, confidential=False,
    )
    prefs = RoutePreferences(require_parameters=True, requested_parameters=frozenset({"tools"}))
    assert catalog_endpoint_candidates(model, prefs) == []


def test_model_overclaims_are_removed_only_when_all_routes_reject_them():
    model = Model(
        id="mistralai/mistral-large", name="Fixture", provider="mistral", context_length=8192,
        supported_parameters=("seed", "reasoning_effort"),
    )
    native = ModelEndpoint(id="native", model_id=model.id, provider="mistral", usage_type="Credits")
    unknown = replace(native, id="unknown", provider="novita")
    corrected, routes = normalize_request_capabilities(model, [native])
    assert corrected.supported_parameters == ()
    assert routes[0].supported_parameters == ("tools",)
    corrected, routes = normalize_request_capabilities(model, [native, unknown])
    assert corrected.supported_parameters == ("seed", "reasoning_effort")
    assert [route.supported_parameters for route in routes] == [("tools",), ()]


@pytest.mark.parametrize(("provider", "efforts", "tools"), [
    ("openai", ["none", "low", "medium", "high", "xhigh"], True),
    ("gmi", None, True),
    ("atlas-cloud", None, False),
])
def test_gpt_55_endpoint_effort_stays_provider_scoped(monkeypatch, provider, efforts, tools):
    model = _model(monkeypatch, "openai/gpt-5.5", "openai")
    endpoint = _route(monkeypatch, model, provider, supported_parameters=())
    assert endpoint_capabilities(model, endpoint) == dict(
        reasoning_effort=efforts, tools=tools, seed=False,
        vision=provider == "openai", confidential=False,
    )


def test_unknown_endpoint_effort_is_json_null_on_both_catalog_routes(client, monkeypatch):
    model = _model(monkeypatch, "deepseek/deepseek-v4-flash-0731", "deepseek")
    _route(monkeypatch, model, "novita", supported_parameters=("tools", "reasoning_effort"))
    expected = dict(reasoning_effort=None, tools=True, seed=False, vision=False, confidential=False)
    response = client.get("/v1/models")
    assert response.status_code == 200
    rows = [row for row in response.json()["data"] if row["id"] == model.id]
    assert len(rows) == 1
    assert rows[0]["trustedrouter"]["capabilities"] == expected
    assert len(rows[0]["trustedrouter"]["endpoints"]) == 1
    assert rows[0]["trustedrouter"]["endpoints"][0]["capabilities"] == expected
    response = client.get(f"/v1/models/{model.id}/endpoints")
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1
    assert response.json()["data"][0]["trustedrouter"]["capabilities"] == expected


@pytest.mark.parametrize(("efforts", "expected"), [
    ([], None),
    ([None], None),
    ([[], []], []),
    ([[], None], None),
    ([["low"], []], ["low"]),
    ([None, ["none", "high"]], ["none", "high"]),
    ([["high", "max"], ["low", "high"], None, []], ["low", "high", "max"]),
])
def test_model_effort_union_distinguishes_rejection_from_unknown(efforts, expected):
    endpoints = [
        dict(reasoning_effort=effort, tools=False, seed=False, vision=False, confidential=False)
        for effort in efforts
    ]
    assert model_capabilities(endpoints, (), ()) == dict(
        reasoning_effort=expected, tools=False, seed=False, vision=False, confidential=False,
    )


def test_model_union_needs_require_parameters_to_select_a_capable_route(monkeypatch):
    model = _model(monkeypatch, "fixture/mixed-capabilities", "deepinfra", modalities=("text", "image"))
    capable = _route(monkeypatch, model, "deepinfra", supported_parameters=("tools", "seed"), input_modalities=("text", "image"))
    limited = _route(monkeypatch, model, "novita", supported_parameters=("max_tokens",), input_modalities=("text",))
    prefs = RoutePreferences(requested_parameters=frozenset({"tools", "seed"}))
    assert {e.id for _, e in catalog_endpoint_candidates(model, prefs)} == {capable.id, limited.id}
    assert [e.id for _, e in catalog_endpoint_candidates(model, replace(prefs, require_parameters=True))] == [capable.id]
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model.id])
    assert row["trustedrouter"]["capabilities"] == dict(reasoning_effort=None, tools=True, seed=True, vision=True, confidential=False)
    by_provider = {e["provider"]: e["capabilities"] for e in row["trustedrouter"]["endpoints"]}
    assert by_provider["deepinfra"] == dict(reasoning_effort=None, tools=True, seed=True, vision=True, confidential=False)
    assert by_provider["novita"] == dict(reasoning_effort=None, tools=False, seed=False, vision=False, confidential=False)
    assert row["architecture"]["input_modalities"] == ["text", "image"]
    assert row["supported_parameters"] == ["tools", "max_tokens", "seed"]


def test_effort_union_preserves_verified_values_and_require_parameters_only_checks_names(monkeypatch):
    model = _model(monkeypatch, "openai/gpt-5", "openai")
    _route(monkeypatch, model, "openai", supported_parameters=("reasoning_effort",))
    _route(monkeypatch, model, "novita", supported_parameters=("reasoning_effort",))
    prefs = RoutePreferences(require_parameters=True, requested_parameters=frozenset({"reasoning_effort"}))
    assert len(catalog_endpoint_candidates(model, prefs)) == 2
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model.id])
    assert row["trustedrouter"]["capabilities"]["reasoning_effort"] == ["minimal", "low", "medium", "high"]
    assert row["trustedrouter"]["endpoints"][0]["capabilities"]["reasoning_effort"] == ["minimal", "low", "medium", "high"]
    # A parameter name is not evidence that a host accepts an enum.
    assert row["trustedrouter"]["endpoints"][1]["capabilities"]["reasoning_effort"] is None
    assert "reasoning_effort" in row["supported_parameters"]


def test_confidential_availability_uses_the_routing_filter_and_current_endpoints(monkeypatch):
    model = _model(monkeypatch, "openai/gpt-oss-120b", "openai", modalities=("text", "image"))
    _route(monkeypatch, model, "deepinfra")
    confidential = _route(monkeypatch, model, "privatemode", upstream_id="gpt-oss-120b")
    prefs = RoutePreferences(min_privacy_rank=PRIVACY_TIER_CONFIDENTIAL)
    assert [e.id for _, e in catalog_endpoint_candidates(model, prefs)] == [confidential.id]
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model.id])
    assert row["trustedrouter"]["capabilities"] == dict(reasoning_effort=["low", "medium", "high"], tools=True, seed=False, vision=False, confidential=True)
    assert [e["capabilities"]["confidential"] for e in row["trustedrouter"]["endpoints"]] == [False, True]
    assert row["trustedrouter"]["endpoints"][1]["capabilities"] == dict(reasoning_effort=["low", "medium", "high"], tools=True, seed=False, vision=False, confidential=True)
    assert row["architecture"]["input_modalities"] == ["text"]
    assert row["architecture"]["modality"] == "text->text"
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, confidential.id, replace(confidential, catalog_valid_until=datetime(2000, 1, 1, tzinfo=UTC)))
    assert catalog_endpoint_candidates(model, prefs) == []
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model.id])
    assert row["trustedrouter"]["capabilities"] == dict(reasoning_effort=None, tools=False, seed=False, vision=False, confidential=False)
    assert len(row["trustedrouter"]["endpoints"]) == 1


@pytest.mark.parametrize(("provider", "upstream", "changes", "expected"), [
    ("privatemode", "glm-5.3", {}, True),
    ("privatemode", "glm-5.3", {"provider_e2ee": False}, False),
    ("privatemode", "glm-5.3", {"provider_zero_data_retention": None, "prepaid_zero_data_retention": False}, False),
    ("privatemode", "glm-5.3", {"provider_confidential_compute": False}, False),
    ("phala", "phala/glm-5.3", {"provider_confidential_compute": True, "provider_e2ee": True, "provider_zero_data_retention": True}, True),
    ("phala", "z-ai/glm-5.3", {"provider_confidential_compute": True, "provider_e2ee": True, "provider_zero_data_retention": True}, False),
    ("zai", "glm-5.3", {"provider_confidential_compute": True, "provider_e2ee": True, "provider_zero_data_retention": True}, False),
])
def test_confidential_is_not_a_provider_badge(monkeypatch, provider, upstream, changes, expected):
    model = _model(monkeypatch, "z-ai/glm-5.3", "zai")
    endpoint = _route(monkeypatch, model, provider, upstream_id=upstream)
    monkeypatch.setitem(catalog.PROVIDERS, provider, replace(catalog.PROVIDERS[provider], **changes))
    matches = catalog_endpoint_candidates(model, RoutePreferences(min_privacy_rank=PRIVACY_TIER_CONFIDENTIAL))
    assert len(matches) == int(expected)
    assert endpoint_capabilities(model, endpoint)["confidential"] is expected


def test_zdr_does_not_imply_confidential(monkeypatch):
    model = _model(monkeypatch, "deepseek/deepseek-v4-flash", "deepseek")
    monkeypatch.setitem(catalog.PROVIDERS, "deepseek", replace(
        catalog.PROVIDERS["deepseek"], provider_zero_data_retention=True,
        provider_confidential_compute=False,
    ))
    endpoint = _route(monkeypatch, model, "deepseek")
    assert catalog.endpoint_privacy_tier(endpoint) == catalog.PRIVACY_TIER_ZERO_RETENTION
    assert endpoint_capabilities(model, endpoint) == dict(reasoning_effort=["high", "max"], tools=False, seed=False, vision=False, confidential=False)


def test_confidential_host_does_not_admit_non_chat_apis(monkeypatch):
    model = _model(monkeypatch, "fixture/embedding", "privatemode")
    model = replace(model, supports_chat=False, supports_embeddings=True)
    endpoint = _route(monkeypatch, model, "privatemode")
    assert catalog.endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)
    assert endpoint_capabilities(model, endpoint)["confidential"] is False


def test_hidden_route_omits_contract_even_with_an_endpoint(monkeypatch):
    model = _model(monkeypatch, "fixture/hidden", "openai")
    _route(monkeypatch, model, "openai", supported_parameters=("tools",))
    assert "capabilities" not in catalog.model_to_openrouter_shape(replace(model, hidden_public_metadata=True))["trustedrouter"]


def test_native_seed_transport_does_not_inherit_generic_parameter_claim():
    model = Model(id="fixture/native", name="Fixture", provider="anthropic", context_length=8192)
    for provider in ("anthropic", "mistral"):
        endpoint = ModelEndpoint(id="fixture", model_id=model.id, provider=provider, usage_type="Credits", supported_parameters=("seed",))
        _, [endpoint] = normalize_request_capabilities(model, [endpoint])
        assert endpoint.supported_parameters == ()
        assert endpoint_capabilities(model, endpoint)["seed"] is False


def test_static_modality_fallback_does_not_override_explicit_text_only_endpoint():
    model = Model(id="fixture/vision", name="Fixture", provider="novita", context_length=8192, input_modalities=("text", "image"))
    endpoint = ModelEndpoint(id="fixture", model_id=model.id, provider="novita", usage_type="Credits")
    _, [endpoint] = normalize_request_capabilities(model, [endpoint])
    assert endpoint.input_modalities == ("text", "image")
    assert endpoint_capabilities(model, endpoint)["vision"] is True
    assert endpoint_capabilities(model, replace(endpoint, input_modalities=("text",)))["vision"] is False


@pytest.mark.parametrize("model_id", ["trustedrouter/auto", "trustedrouter/fast", "trustedrouter/confidential"])
def test_routing_aliases_omit_unprovable_contract(model_id):
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model_id])
    assert "capabilities" not in row["trustedrouter"]


def test_empty_route_pool_does_not_vacuously_support_everything(monkeypatch):
    model = _model(monkeypatch, "fixture/empty-capabilities", "openai")
    assert catalog.model_to_openrouter_shape(catalog.MODELS[model.id])["trustedrouter"]["capabilities"] == dict(reasoning_effort=None, tools=False, seed=False, vision=False, confidential=False)


def test_mistral_does_not_gain_effort_or_seed_from_overbroad_manifest(monkeypatch):
    model = _model(monkeypatch, "mistralai/mistral-large", "mistral")
    _route(monkeypatch, model, "mistral", supported_parameters=("tools", "reasoning_effort", "seed"))
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model.id])
    assert row["supported_parameters"] == ["tools", "max_tokens"]
    assert row["trustedrouter"]["endpoints"][0]["supported_parameters"] == ["tools"]
    assert row["trustedrouter"]["capabilities"]["reasoning_effort"] == []
    assert row["trustedrouter"]["capabilities"]["seed"] is False
    _route(monkeypatch, model, "novita", supported_parameters=("seed",))
    row = catalog.model_to_openrouter_shape(catalog.MODELS[model.id])
    assert row["supported_parameters"] == ["tools", "max_tokens", "seed"]
    assert row["trustedrouter"]["capabilities"] == dict(reasoning_effort=None, tools=True, seed=True, vision=False, confidential=False)


def test_capability_docs_explain_scope_and_unknown_support(client: TestClient):
    response = client.get("/docs", headers={"accept": "text/html"})
    assert response.status_code == 200
    for text in (
        "trustedrouter.capabilities", "trustedrouter.endpoints[].capabilities", "Mistral Large",
        "model row's final", "union of verified lists",
        "every route is verified to reject effort", "null</code> means not verified",
        "[]</code> means do not send effort", "Default routing does not filter by parameters",
        "provider.require_parameters: true", "include every parameter sent",
        "Check the per-route values first", "some routes declare less than the model supports",
        "not effort values or image input", "provider.min_privacy", "Routing aliases",
    ):
        assert text in response.text


def test_public_openapi_explains_capability_union_and_parameter_filter(client: TestClient):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    description = response.json()["paths"]["/v1/models"]["get"]["description"]
    for text in (
        "agrees with model discovery declarations", "ordered union of verified values",
        "every route is verified to reject effort", "null means not verified",
        "[] means do not send reasoning_effort", "Default routing does not filter by parameters",
        "Some routes declare less than the model supports", "provider.require_parameters: true",
        "supported_parameters include every parameter sent", "not effort values or image input",
        "trustedrouter.endpoints[].capabilities", "Routing aliases omit the object",
    ):
        assert text in description


def _assert_reviewed_contracts(contracts):
    routes = []
    for contract in contracts:
        if "reasoning_effort" in contract:
            assert contract["reasoning_effort"] == [v for v in EFFORT_ORDER if v in contract["reasoning_effort"]]
        assert contract["source"]
        routes.extend((provider, model) for provider in contract["providers"] for model in contract["models"])
    assert len(routes) == len(set(routes))


def test_reviewed_contracts_have_unique_routes_and_canonical_efforts():
    data = json.loads(Path("src/trusted_router/data/request_capabilities.json").read_text())
    _assert_reviewed_contracts(data["contracts"])


def test_catalog_efforts_agree_with_gateway_wire_contract_vectors():
    data = json.loads(Path("tests/fixtures/gateway_effort_contract.json").read_text())
    for case in data["cases"]:
        model = Model(id=case["model"], name="Fixture", provider=case["provider"], context_length=8192)
        endpoint = ModelEndpoint(id="fixture", model_id=model.id, provider=case["provider"], usage_type="Credits", upstream_id=case["upstream_id"])
        accepted = [effort for effort in EFFORT_ORDER if case["wire"][effort] in case["upstream_accepts"]]
        assert endpoint_capabilities(model, endpoint)["reasoning_effort"] == accepted, case


def test_native_endpoint_modalities_do_not_inherit_other_hosts_images(monkeypatch, tmp_path):
    for provider, modalities in [("deepinfra", ["text", "image"]), ("novita", ["text"])]:
        (tmp_path / f"{provider}.json").write_text(json.dumps({
            "provider": provider, "models": [{
                "id": "fixture/mixed-vision", "upstream_id": "mixed-vision",
                "model_type": "chat", "endpoints": ["chat/completions"],
                "input_modalities": modalities,
                "input_token_price_per_m": 1000000, "output_token_price_per_m": 2000000,
            }],
        }))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert len(models) == 1
    assert len(endpoints) == 4
    model = models["fixture/mixed-vision"]
    assert "image" in model.input_modalities
    assert {e.provider: e.input_modalities for e in endpoints.values()} == {
        "deepinfra": ("text", "image"), "novita": ("text",),
    }
    assert {e.provider: endpoint_capabilities(model, e)["vision"] for e in endpoints.values()} == {
        "deepinfra": True, "novita": False,
    }


def test_snapshot_endpoint_modalities_override_model_architecture(monkeypatch, tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": [{
        "id": "fixture/snapshot-vision",
        "architecture": {"input_modalities": ["text", "image"]},
        "endpoints": [
            {"tr_provider_slug": provider, "model_id": "snapshot-vision",
             **({"input_modalities": modalities} if modalities is not None else {}),
             "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
            for provider, modalities in [("deepinfra", ["text", "image"]), ("novita", None)]
        ],
    }]}))
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", manifests)
    models, endpoints = catalog_ingest._ingested_models_and_endpoints()
    assert len(models) == 1
    assert len(endpoints) == 4
    assert {e.provider: e.input_modalities for e in endpoints.values()} == {
        "deepinfra": ("text", "image"), "novita": ("text",),
    }


def test_picker_reads_objects_without_overriding_false_with_name_guesses():
    node = shutil.which("node")
    assert node is not None, "Node is required to exercise the actual catalog picker"
    script = """
global.window = {};
require('./src/trusted_router/static/model_catalog.js');
const normalize = window.TrustedRouterModelCatalog.normalizeModel;
const rows = [
  {id: 'openai/gpt-5', trustedrouter: {capabilities: {tools: false, seed: false, vision: false, confidential: false, reasoning_effort: []}}},
  {id: 'deepseek/deepseek-v4-flash-0731', trustedrouter: {capabilities: {tools: true, seed: false, vision: false, confidential: false, reasoning_effort: null}}},
  {id: 'fixture/positive', trustedrouter: {capabilities: {tools: true, seed: true, vision: true, confidential: true, reasoning_effort: ['low']}}},
  {id: 'fixture/legacy', trustedrouter: {capabilities: ['seed']}},
  {id: 'fixture/unknown', trustedrouter: {}}
];
process.stdout.write(JSON.stringify(rows.map(row => normalize(row).capabilities)));
"""
    result = subprocess.run(  # noqa: S603 - local JS, no network or user input
        [node, "-e", script], check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == [
        [], ["tools"], ["tools", "seed", "vision", "confidential", "reasoning"], ["seed"], [],
    ]
