from __future__ import annotations

import json

from fastapi.testclient import TestClient

from trusted_router import catalog_ingest
from trusted_router.catalog import MODELS, model_to_openrouter_shape
from trusted_router.catalog_capabilities import manifest_supported_parameters


def test_manifest_capabilities_preserve_explicit_parameters_and_features() -> None:
    supported = manifest_supported_parameters(
        {
            "supported_parameters": ["temperature", "top_p"],
            "supported_sampling_parameters": ["seed", "temperature"],
            "features": [
                "function-calling",
                "structured-outputs",
                "response-format",
            ],
            "supports_reasoning": True,
        }
    )

    assert supported == (
        "tools",
        "max_tokens",
        "temperature",
        "top_p",
        "reasoning",
        "include_reasoning",
        "structured_outputs",
        "response_format",
        "seed",
    )


def test_manifest_capabilities_do_not_invent_tool_choice() -> None:
    supported = manifest_supported_parameters({"features": ["function-calling"]})

    assert "tools" in supported
    assert "tool_choice" not in supported


def test_native_endpoint_replaces_stale_snapshot_model_capabilities(monkeypatch, tmp_path) -> None:
    model_id = "x-ai/grok-4.7"
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": [{
        "id": model_id,
        "endpoints": [{
            "tr_provider_slug": "grok", "model_id": "grok-4.7",
            "pricing": {"prompt": "0.000002", "completion": "0.000006"},
            "supported_parameters": ["logprobs", "top_logprobs"],
        }],
    }]}))
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    models, endpoints = catalog_ingest._ingested_models_and_endpoints()
    assert "tools" in models[model_id].supported_parameters
    assert not {"logprobs", "top_logprobs"} & set(models[model_id].supported_parameters)
    assert all(endpoint.supported_parameters == models[model_id].supported_parameters
               for endpoint in endpoints.values())


def test_native_capability_declaration_is_provider_scoped_and_explicit(monkeypatch, tmp_path):
    for provider, row in {
        "grok": {"supported_parameters": ["tools"]},
        "other": {"supported_parameters": ["logprobs"]},
        "partial": {"features": ["function-calling"]},
        "empty": {"supported_parameters": []},
        "invalid": {"supported_parameters": [7]},
    }.items():
        (tmp_path / f"{provider}.json").write_text(json.dumps({
            "provider": provider, "models": [{"id": "model", **row}],
        }))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    assert catalog_ingest._native_endpoint_capabilities() == {
        ("grok", "model"): ("tools", "max_tokens"),
        ("other", "model"): ("max_tokens", "logprobs"),
        ("empty", "model"): ("max_tokens",),
    }


def test_public_models_publish_openrouter_supported_parameters(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200
    models = response.json()["data"]
    assert models
    assert all(isinstance(model["supported_parameters"], list) for model in models)
    assert all(
        model["supported_parameters"] for model in models if model["trustedrouter"]["supports_chat"]
    )

    by_id = {model["id"]: model for model in models}
    sonnet = by_id["anthropic/claude-sonnet-5"]
    assert {"tools", "tool_choice", "reasoning", "structured_outputs"}.issubset(
        sonnet["supported_parameters"]
    )


def test_model_capabilities_are_union_of_routable_endpoints() -> None:
    model = MODELS["openai/gpt-oss-120b"]
    shape = model_to_openrouter_shape(model)
    endpoint_parameters = {
        parameter
        for endpoint in shape["trustedrouter"]["endpoints"]
        for parameter in endpoint["supported_parameters"]
    }

    assert endpoint_parameters.issubset(set(shape["supported_parameters"]))
    assert {"tools", "reasoning", "response_format"}.issubset(shape["supported_parameters"])


def test_model_endpoints_publish_endpoint_specific_parameters(client: TestClient) -> None:
    response = client.get("/v1/models/anthropic/claude-sonnet-5/endpoints")

    assert response.status_code == 200
    endpoints = response.json()["data"]
    assert endpoints
    assert all(isinstance(endpoint["supported_parameters"], list) for endpoint in endpoints)
    assert any("tools" in endpoint["supported_parameters"] for endpoint in endpoints)


def test_models_filter_requires_every_requested_supported_parameter(
    client: TestClient,
) -> None:
    response = client.get("/v1/models?supported_parameters=tools,reasoning")

    assert response.status_code == 200
    models = response.json()["data"]
    assert models
    assert all({"tools", "reasoning"}.issubset(model["supported_parameters"]) for model in models)
    assert client.get("/v1/models/count?supported_parameters=tools,reasoning").json()["data"][
        "count"
    ] == len(models)
