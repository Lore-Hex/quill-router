from __future__ import annotations

from fastapi.testclient import TestClient

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
