"""Provider-owned declarations, using trimmed 2026-10-07 API payloads."""

from __future__ import annotations

import json
import runpy
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import (
    _direct_openai,
    atlas_cloud,
    deepinfra,
    featherless,
    fireworks,
    friendli,
    wafer,
)
from trusted_router import catalog, catalog_ingest, catalog_registry
from trusted_router.request_capabilities import normalize_request_capabilities
from trusted_router.routing import RoutePreferences, catalog_endpoint_candidates

FIXTURES = Path(__file__).parent / "fixtures/provider_capabilities"
LIST_URL = "https://api.deepinfra.com/models/list"
CASES = [
    (deepinfra, "deepinfra_models_list", {"tools", "structured-outputs", "json-mode", "reasoning"}),
    (featherless, "featherless_models_subset", {"tools"}),
    (atlas_cloud, "atlas-cloud_models", {"tools", "structured_outputs", "json_mode", "reasoning"}),
    (fireworks, "fireworks_models", {"tools"}),
    (friendli, "friendli_models", {"tools", "tool-choice", "parallel-tool-calls", "structured-outputs", "reasoning"}),
    (wafer, "wafer_models", {"tools", "structured-outputs", "json-mode", "reasoning"}),
]
# Expected canonical/native identities are independent of the fetchers' maps.
# The first pair receives a declaration; the second is an undeclared control.
MODEL_KEYS = {
    "deepinfra": (("z-ai/glm-5.3", "zai-org/GLM-5.3"),
                  ("openchat/openchat-3.6-8b", "openchat/openchat-3.6-8b")),
    "featherless": (("moonshotai/kimi-k2.5", "moonshotai/Kimi-K2.5"),
                    ("xiaomi/mimo-v2.5", "XiaomiMiMo/MiMo-V2.5")),
    "atlas-cloud": (("deepseek/deepseek-v3.2-exp", "deepseek-ai/DeepSeek-V3.2-Exp"),
                    ("google/gemini-2.5-flash", "google/gemini-2.5-flash")),
    "fireworks": (("z-ai/glm-5.3", "accounts/fireworks/models/glm-5p3"),
                  ("z-ai/glm-5.3-flash", "accounts/fireworks/models/glm-5p3-flash")),
    "friendli": (("z-ai/glm-5.3", "zai-org/GLM-5.3"),
                 ("z-ai/glm-5.3-flash", "zai-org/GLM-5.3-Flash")),
    "wafer": (("z-ai/glm-5.3", "GLM-5.3"), ("z-ai/glm-5.3-flash", "GLM-5.3-Flash")),
}
FIELD_CASES = [
    (deepinfra, "deepinfra_models_list", "tools", "tools"),
    (deepinfra, "deepinfra_models_list", "json", "json-mode"),
    (deepinfra, "deepinfra_models_list", "structured-output", "structured-outputs"),
    (deepinfra, "deepinfra_models_list", "reasoning", "reasoning"),
    (featherless, "featherless_models_subset", "tool_use", "tools"),
    (atlas_cloud, "atlas-cloud_models", "tools", "tools"),
    (atlas_cloud, "atlas-cloud_models", "json_mode", "json_mode"),
    (atlas_cloud, "atlas-cloud_models", "structured_outputs", "structured_outputs"),
    (atlas_cloud, "atlas-cloud_models", "reasoning", "reasoning"),
    (fireworks, "fireworks_models", "supports_tools", "tools"),
    (friendli, "friendli_models", "tool_call", "tools"),
    (friendli, "friendli_models", "tool_choice", "tool-choice"),
    (friendli, "friendli_models", "parallel_tool_call", "parallel-tool-calls"),
    (friendli, "friendli_models", "structured_output", "structured-outputs"),
    (friendli, "friendli_models", "reasoning", "reasoning"),
    (wafer, "wafer_models", "tools", "tools"),
    (wafer, "wafer_models", "json_object", "json-mode"),
    (wafer, "wafer_models", "json_schema", "structured-outputs"),
    (wafer, "wafer_models", "reasoning", "reasoning"),
]


def _payload(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _set_declarations(provider, row, declaration):
    if provider is deepinfra:
        row["tags"] = ["openai", "non-reasoning"] if declaration is False else declaration
    elif provider is featherless:
        row["features"] = {"tool_use": declaration}
    elif provider is atlas_cloud:
        row["supported_features"] = [] if declaration is False else declaration
    elif provider is fireworks:
        row["supports_tools"] = declaration
    elif provider is friendli:
        row["functionality"] = dict.fromkeys(row["functionality"], declaration)
        row["reasoning"] = declaration
    else:
        caps = row["wafer"]["capabilities"]
        caps["chat_completions"] = dict.fromkeys(caps["chat_completions"], declaration)
        caps["reasoning"] = declaration
        # Other Wafer APIs still declare tools; they do not prove chat support.


def _manifest_features(manifest):
    return {(row["id"], row["upstream_id"]): set(row.get("supported_features", []))
            for row in manifest}


def _fetch_manifest(monkeypatch, tmp_path, provider, payload, *, list_failure=None, existing_features=None, reset_manifest=True):
    """Run the real fetch + writer; all transports and paid probes stay offline."""
    path = tmp_path / f"{provider.SLUG}.json"
    if reset_manifest:
        path.write_text(json.dumps({"provider": provider.SLUG, "models": []}))
    monkeypatch.setattr(provider, "MANIFEST_PATH", path)
    rows = payload if isinstance(payload, list) else payload["data"]
    primary = payload
    if provider is deepinfra:
        # Only the public tag response was captured. These are transport/pricing
        # stubs for its separate OpenAI listing, with the exact native join keys.
        primary = {"data": [
            {"id": row["model_name"], "metadata": {
                "pricing": {"input_tokens": 1, "output_tokens": 2},
            }} for row in reversed(rows)
        ]}

    def respond(request):
        if str(request.url) == LIST_URL:
            assert "authorization" not in request.headers
            if list_failure == "timeout":
                raise httpx.ReadTimeout("fixture timeout", request=request)
            if list_failure == "http":
                return httpx.Response(503)
            if list_failure == "json":
                return httpx.Response(200, text="invalid json")
            if list_failure == "shape":
                return httpx.Response(200, json={"unexpected": []})
            return httpx.Response(200, json=payload)
        assert str(request.url) == provider.URL
        return httpx.Response(200, json=primary)

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setenv(f"{provider.SLUG.upper().replace('-', '_')}_API_KEY", "fixture-key")
    if provider is featherless:
        monkeypatch.setattr(featherless.CATALOG, "manifest_path", path)
        monkeypatch.setattr(featherless.CATALOG, "discovered_rows", {})
        monkeypatch.setattr(featherless.CATALOG, "upstream_id_map", dict(featherless.UPSTREAM_ID_MAP))
        monkeypatch.setattr(featherless.CATALOG, "_fetched", False)
        monkeypatch.setattr(featherless.CATALOG, "spec", replace(
            featherless.CATALOG.spec, expected_models=(), catalog_loader=lambda _key: rows,
        ))
        monkeypatch.setattr(featherless, "_published_prices", lambda _html: ({}, {}))
        monkeypatch.setattr(featherless, "fetch_html", lambda _url: "")
        monkeypatch.setattr(_direct_openai, "models_requiring_canary", lambda *_a, **_k: set())
    else:
        monkeypatch.setattr(provider, "EXPECTED_MODELS", [])
        monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {})
        monkeypatch.setattr(provider, "UPSTREAM_ID_MAP", dict(provider.UPSTREAM_ID_MAP))
    if provider is fireworks:
        prices = {
            fireworks._NATIVE_TO_CANONICAL[row["id"]]: ModelPrice(1_000_000, 2_000_000)
            for row in rows if row["id"] in fireworks._NATIVE_TO_CANONICAL
        }
        monkeypatch.setattr(fireworks, "fetch_json", lambda *_a, **_k: payload)
        monkeypatch.setattr(fireworks, "fetch_provider", lambda **_k: ProviderPricingResult(
            slug="fireworks", prices=prices, source="api", fetched_url=fireworks.URL, notes=[],
        ))
    result = provider.fetch()
    assert result.prices
    if existing_features is not None:
        path.write_text(json.dumps({"provider": provider.SLUG, "models": [
            {"id": model_id, "features": existing_features} for model_id in result.prices
        ]}))
    provider.write_provider_manifest(result)
    return json.loads(path.read_text())["models"]


@pytest.mark.parametrize(("provider", "fixture", "expected"), CASES, ids=[c[0].SLUG for c in CASES])
def test_fetcher_writes_only_positive_provider_declarations(monkeypatch, tmp_path, provider, fixture, expected):
    original = _payload(fixture)
    declared_key, control_key = MODEL_KEYS[provider.SLUG]
    original_rows = original if isinstance(original, list) else original["data"]
    # Some captures declare every model. Withdraw the control's declarations
    # without changing its real model ID, pricing, or unrelated capabilities.
    for row in original_rows:
        if row.get("model_name", row.get("id")) == control_key[1]:
            _set_declarations(provider, row, False)
    # Keep the model IDs fixed while removing/changing evidence: no model-name
    # inference, truthiness of "false"/1, or stale discovery state may add labels.
    for declaration in (True, False, None, "false", 1):
        payload = deepcopy(original)
        rows = payload if isinstance(payload, list) else payload["data"]
        if declaration is not True:
            for row in rows:
                _set_declarations(provider, row, declaration)
        with monkeypatch.context() as patch:
            manifest = _fetch_manifest(patch, tmp_path, provider, payload)
        assert _manifest_features(manifest) == {
            declared_key: expected if declaration is True else set(), control_key: set(),
        }
        if provider is atlas_cloud and declaration is True:
            assert any("seed" in row.get("supported_sampling_parameters", []) for row in manifest)
    with monkeypatch.context() as patch:
        manifest = _fetch_manifest(
            patch, tmp_path, provider, original, existing_features=["logprobs"],
        )
    assert _manifest_features(manifest) == {declared_key: expected, control_key: set()}
    assert all(row["features"] == ["logprobs"] for row in manifest)


@pytest.mark.parametrize(
    ("provider", "fixture", "field", "label"), FIELD_CASES,
    ids=[f"{provider.SLUG}-{field}" for provider, _fixture, field, _label in FIELD_CASES],
)
def test_fetcher_maps_each_provider_field_independently(
    monkeypatch, tmp_path, provider, fixture, field, label,
):
    payload = _payload(fixture)
    rows = payload if isinstance(payload, list) else payload["data"]
    declared_key, control_key = MODEL_KEYS[provider.SLUG]
    for row in rows:
        _set_declarations(provider, row, False)
        if row.get("model_name", row.get("id")) != declared_key[1]:
            continue
        if provider is deepinfra:
            row["tags"].append(field)
        elif provider is featherless:
            row["features"][field] = True
        elif provider is atlas_cloud:
            row["supported_features"].append(field)
        elif provider is fireworks:
            row[field] = True
        elif provider is friendli:
            (row if field == "reasoning" else row["functionality"])[field] = True
        else:
            caps = row["wafer"]["capabilities"]
            (caps if field == "reasoning" else caps["chat_completions"])[field] = True
    manifest = _fetch_manifest(monkeypatch, tmp_path, provider, payload)
    assert _manifest_features(manifest) == {declared_key: {label}, control_key: set()}


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "shape"])
def test_deepinfra_optional_list_failure_preserves_refresh(monkeypatch, tmp_path, caplog, failure):
    manifest = _fetch_manifest(
        monkeypatch, tmp_path, deepinfra, _payload("deepinfra_models_list"), list_failure=failure,
    )
    assert len(manifest) == 2
    assert all(row["input_token_price_per_m"] == 1_000_000 for row in manifest)
    assert all(not row.get("features") for row in manifest)
    assert all("supported_features" not in row for row in manifest)
    assert "DeepInfra" in caplog.text and "/models/list" in caplog.text


@pytest.mark.parametrize(("provider", "fixture", "expected"), CASES, ids=[c[0].SLUG for c in CASES])
def test_refresh_replaces_provider_labels_but_preserves_curated_features(
    monkeypatch, tmp_path, provider, fixture, expected,
):
    payload = _payload(fixture)
    with monkeypatch.context() as patch:
        manifest = _fetch_manifest(
            patch, tmp_path, provider, payload, existing_features=["logprobs"],
        )
    # Start with a previous provider declaration in its owned field. This also
    # exercises withdrawal on the pre-change writer, which ratchets that field.
    for row in manifest:
        row["features"] = ["logprobs"]
        row["supported_features"] = sorted(expected)
    (tmp_path / f"{provider.SLUG}.json").write_text(json.dumps({
        "provider": provider.SLUG, "models": manifest,
    }))
    rows = payload if isinstance(payload, list) else payload["data"]
    for row in rows:
        if provider is deepinfra:
            row["tags"] = ["openai"]
        elif provider is featherless:
            row["features"] = {"tool_use": False}
        elif provider is atlas_cloud:
            row["supported_features"] = []
        elif provider is fireworks:
            row["supports_tools"] = False
        elif provider is friendli:
            row["functionality"] = {}
            row["reasoning"] = False
        else:
            row["wafer"]["capabilities"]["chat_completions"] = {}
            row["wafer"]["capabilities"]["reasoning"] = False
    with monkeypatch.context() as patch:
        refreshed = _fetch_manifest(patch, tmp_path, provider, payload, reset_manifest=False)
    assert refreshed
    assert all(row["supported_features"] == [] for row in refreshed)
    assert all(row["features"] == ["logprobs"] for row in refreshed)


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "shape"])
def test_deepinfra_failed_list_retains_previous_provider_labels(monkeypatch, tmp_path, caplog, failure):
    payload = _payload("deepinfra_models_list")
    with monkeypatch.context() as patch:
        manifest = _fetch_manifest(patch, tmp_path, deepinfra, payload)
    assert any("tools" in row.get("supported_features", []) for row in manifest)
    for row in manifest:
        row["features"] = ["logprobs"]
    previous = {row["id"]: row["supported_features"] for row in manifest}
    (tmp_path / "deepinfra.json").write_text(json.dumps({"provider": "deepinfra", "models": manifest}))
    refreshed = _fetch_manifest(
        monkeypatch, tmp_path, deepinfra, payload, list_failure=failure, reset_manifest=False,
    )
    assert {row["id"]: row["supported_features"] for row in refreshed} == previous
    assert all(row["features"] == ["logprobs"] for row in refreshed)
    assert all("supported_features" not in row for row in deepinfra._DISCOVERED_MANIFEST_ROWS.values())
    assert "/models/list" in caplog.text


@pytest.mark.parametrize("provider", [case[0].SLUG for case in CASES])
def test_provider_features_reach_snapshot_and_manifest_routes(monkeypatch, tmp_path, provider):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    model_id = "fixture/provider-tools"
    rows = [
        {"id": model_id, "upstream_id": "provider-tools", "model_type": "chat",
         "endpoints": ["chat/completions"], "input_token_price_per_m": 1_000_000,
         "output_token_price_per_m": 2_000_000, **declaration}
        for declaration in ({"supported_features": ["tools"]}, {})
    ]
    for slug, row in zip((provider, "novita"), rows, strict=True):
        (manifests / f"{slug}.json").write_text(json.dumps({
            "provider": slug, "generated_at": "2026-10-07T00:00:00Z", "models": [row],
        }))
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": [{
        "id": model_id, "supported_parameters": ["tools"],
        "endpoints": [
            {"tr_provider_slug": slug, "model_id": "provider-tools",
             "pricing": {"prompt": "0.000001", "completion": "0.000002"},
             "supported_parameters": ["temperature", "provider_extension"]}
            for slug in (provider, "novita")
        ],
    }]}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", manifests)
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    snapshot_models, snapshot_endpoints = catalog_ingest._ingested_models_and_endpoints()
    manifest_models, manifest_endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    for models, endpoints, keep_snapshot_parameters in (
        (snapshot_models, snapshot_endpoints, True),
        (manifest_models, manifest_endpoints, False),
    ):
        expected_count = 3 + int(catalog.PROVIDERS[provider].supports_byok)
        assert len(endpoints) == expected_count
        model, normalized = normalize_request_capabilities(models[model_id], list(endpoints.values()))
        monkeypatch.setitem(catalog.MODELS, model_id, model)
        for endpoint in normalized:
            monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, endpoint)
            if keep_snapshot_parameters:
                assert {"temperature", "provider_extension"} <= set(endpoint.supported_parameters)
        shape = catalog.model_to_openrouter_shape(model)
        published = shape["trustedrouter"]["endpoints"]
        assert len(published) == expected_count
        assert {row["provider"]: row["capabilities"]["tools"] for row in published} == {
            provider: True, "novita": False,
        }
        assert all(("tools" in row["supported_parameters"]) == (row["provider"] == provider)
                   for row in published)
        matches = catalog_endpoint_candidates(model, RoutePreferences(
            require_parameters=True, requested_parameters=frozenset({"tools"}),
        ))
        assert {endpoint.id for _, endpoint in matches} == {
            endpoint.id for endpoint in normalized if endpoint.provider == provider
        }


@pytest.mark.parametrize("surface", ["registry", "models-api"])
def test_final_registry_publishes_replacing_manifest_route_features(monkeypatch, tmp_path, client, surface):
    from tests.fixture_routes import bypass_catalog_caches

    model_id = "fixture/registry-provider-tools"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "deepinfra.json").write_text(json.dumps({
        "provider": "deepinfra", "generated_at": "2026-10-07T00:00:00Z", "models": [{
            "id": model_id, "upstream_id": "registry-provider-tools", "model_type": "chat",
            "endpoints": ["chat/completions"], "input_token_price_per_m": 1_000_000,
            "output_token_price_per_m": 2_000_000, "supported_features": ["tools"],
        }],
    }))
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": [{"id": model_id, "endpoints": [{
        "tr_provider_slug": "deepinfra", "model_id": "registry-provider-tools",
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "supported_parameters": ["temperature", "provider_extension"],
    }]}]}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", manifests)
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    # Execute the actual construction/merge/normalization in a fresh namespace;
    # reloading the live module would leave other tests holding stale registries.
    registry = runpy.run_path(catalog_registry.__file__)
    expected_ids = {f"{model_id}@deepinfra/prepaid", f"{model_id}@deepinfra/byok"}
    ingested = {key: ep for key, ep in registry["_INGESTED_ENDPOINTS"].items()
                if ep.model_id == model_id}
    supplemental = {key: ep for key, ep in registry["_SUPPLEMENTAL_ENDPOINTS"].items()
                    if ep.model_id == model_id}
    registered = {key: ep for key, ep in registry["MODEL_ENDPOINTS"].items()
                  if ep.model_id == model_id}
    assert set(ingested) == set(supplemental) == set(registered) == expected_ids
    assert all("provider_extension" in ep.supported_parameters for ep in ingested.values())
    # The existing merge replaces the snapshot row. Do not require retention of
    # snapshot-only parameters: this test pins the provider-owned declaration.
    if surface == "registry":
        assert {key: set(ep.supported_parameters) for key, ep in registered.items()} == {
            key: {"max_tokens", "tools"} for key in expected_ids
        }
        return
    monkeypatch.setitem(catalog.MODELS, model_id, registry["MODELS"][model_id])
    for key, endpoint in registered.items():
        monkeypatch.setitem(catalog.MODEL_ENDPOINTS, key, endpoint)
    bypass_catalog_caches(monkeypatch)
    response = client.get("/v1/models")
    assert response.status_code == 200
    published = next(row for row in response.json()["data"] if row["id"] == model_id)
    routes = published["trustedrouter"]["endpoints"]
    assert {row["id"]: (set(row["supported_parameters"]), row["capabilities"]["tools"])
            for row in routes} == {
        key: ({"max_tokens", "tools"}, True) for key in expected_ids
    }


@pytest.mark.parametrize("publisher_manifest", [False, True], ids=["absent", "held"])
def test_partial_labels_do_not_leak_to_synthesized_publisher_routes(
    monkeypatch, tmp_path, publisher_manifest,
):
    model_id = "z-ai/fixture-tool-scope"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for provider in (["deepinfra", "zai"] if publisher_manifest else ["deepinfra"]):
        (manifests / f"{provider}.json").write_text(json.dumps({
            "provider": provider, "models": [{
                "id": model_id, "supported_features": ["tools"],
                "routable": provider == "deepinfra",
            }],
        }))
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": [{"id": model_id, "endpoints": [{
        "tr_provider_slug": "deepinfra", "model_id": "fixture-tool-scope",
        "supported_parameters": ["max_tokens"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }]}]}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", manifests)
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    models, ingested = catalog_ingest._ingested_models_and_endpoints()
    synthesized = catalog_ingest._build_endpoints(models)
    assert len(synthesized) == 2
    assert {ep.provider for ep in synthesized.values()} == {"zai"}
    assert all(ep.supported_parameters == ("max_tokens",) for ep in synthesized.values())
    model, endpoints = normalize_request_capabilities(
        models[model_id], [*synthesized.values(), *ingested.values()],
    )
    monkeypatch.setitem(catalog.MODELS, model_id, model)
    for endpoint in endpoints:
        monkeypatch.setitem(catalog.MODEL_ENDPOINTS, endpoint.id, endpoint)
    published = catalog.model_to_openrouter_shape(model)
    assert published["trustedrouter"]["capabilities"]["tools"] is True
    assert {row["provider"]: row["capabilities"]["tools"]
            for row in published["trustedrouter"]["endpoints"]} == {"deepinfra": True, "zai": False}


def test_kimi_k3_efforts_match_provider_model_api():
    from trusted_router.request_capabilities import _reviewed_contracts

    declared = _payload("kimi_models")["data"][0]["reasoning_efforts"]["valid_efforts"]
    contract = _reviewed_contracts()[("kimi", "moonshotai/kimi-k3")]
    assert contract["reasoning_effort"] == declared == ["low", "high", "max"]
    # The same row also records its tools source, appended after the effort source.
    assert contract["source"].startswith(
        "Moonshot GET /v1/models reasoning_efforts.valid_efforts (2026-10-07); "
        "paid probe 2026-10-06 accepted low and high"
    )
