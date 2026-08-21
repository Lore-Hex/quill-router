from __future__ import annotations

from fastapi.testclient import TestClient
from httpx import Response

from trusted_router.catalog_registry import MODELS
from trusted_router.image_generation import IMAGE_MODEL_ID_SET, IMAGE_MODEL_SPECS
from trusted_router.storage import STORE


def test_image_catalog_is_machine_readable_and_matches_general_filter(
    client: TestClient,
) -> None:
    response = client.get("/v1/images/models")
    assert response.status_code == 200, response.text
    models = response.json()["data"]
    assert {model["id"] for model in models} == IMAGE_MODEL_ID_SET
    for model in models:
        spec = IMAGE_MODEL_SPECS[model["id"]]
        assert model["architecture"]["output_modalities"] == ["image"]
        assert model["supported_parameters"] == spec.parameters()
        assert "output_format" not in model["supported_parameters"]
        assert "seed" not in model["supported_parameters"]
        assert model["supports_streaming"] is spec.supports_streaming
        assert model["endpoints"] == f"/v1/images/models/{model['id']}/endpoints"

    by_id = {model["id"]: model for model in models}
    assert by_id["google/gemini-3.1-flash-image"]["architecture"][
        "input_modalities"
    ] == ["text", "image"]
    assert by_id["openai/gpt-image-2"]["architecture"]["input_modalities"] == [
        "text"
    ]
    assert by_id["openai/gpt-image-2"]["architecture"]["modality"] == "text->image"
    assert by_id["openai/gpt-image-2"]["supported_parameters"]["n"]["max"] == 10
    assert by_id["x-ai/grok-imagine-image-2.0"]["supported_parameters"][
        "quality"
    ]["values"] == ["low", "medium"]

    filtered = client.get("/v1/models", params={"output_modalities": "image"})
    assert filtered.status_code == 200, filtered.text
    assert {model["id"] for model in filtered.json()["data"]} == IMAGE_MODEL_ID_SET
    assert MODELS["google/gemini-3.1-flash-image"].supports_chat is False


def test_image_endpoint_catalog_reports_resolution_prices(client: TestClient) -> None:
    response = client.get(
        "/v1/images/models/google/gemini-3.1-flash-image/endpoints"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["id"] == "google/gemini-3.1-flash-image"
    assert payload["endpoints"]
    endpoint = payload["endpoints"][0]
    assert endpoint["provider_slug"] == "google-ai-studio"
    assert endpoint["provider_tag"] == "google-ai-studio"
    assert endpoint["allowed_passthrough_parameters"] == []
    assert endpoint["supports_streaming"] is False
    input_prices = {
        row["billable"]: row["cost_usd"]
        for row in endpoint["pricing"]
        if row["unit"] == "token"
    }
    assert input_prices == {
        "input_text": 5.275e-07,
        "input_image": 5.275e-07,
    }
    prices = {
        row["variant"]: row["cost_usd"]
        for row in endpoint["pricing"]
        if row["billable"] == "output_image"
    }
    assert prices == {
        "512": 0.0472851,
        "1k": 0.070896,
        "2k": 0.106344,
        "4k": 0.159516,
    }

    openai = client.get("/v1/images/models/openai/gpt-image-2/endpoints").json()
    assert len(openai["endpoints"]) == 2
    openai_credits = next(
        endpoint
        for endpoint in openai["endpoints"]
        if endpoint["trustedrouter"]["usage_type"] == "Credits"
    )
    assert openai_credits["provider_slug"] == "openai"
    assert openai_credits["allowed_passthrough_parameters"] == ["moderation"]
    assert openai_credits["supported_parameters"]["n"]["max"] == 10
    assert openai_credits["pricing"] == [
        {"billable": "input_text", "unit": "token", "cost_usd": 5.275e-06},
        {"billable": "output_image", "unit": "token", "cost_usd": 3.165e-05},
    ]

    grok = client.get(
        "/v1/images/models/x-ai/grok-imagine-image-2.0/endpoints"
    ).json()
    grok_credits = next(
        endpoint
        for endpoint in grok["endpoints"]
        if endpoint["trustedrouter"]["usage_type"] == "Credits"
    )
    assert grok_credits["provider_slug"] == "grok"
    assert grok_credits["pricing"] == [
        {
            "billable": "output_image",
            "unit": "image",
            "variant": "low_1k",
            "cost_usd": 0.0422,
        },
        {
            "billable": "output_image",
            "unit": "image",
            "variant": "low_2k",
            "cost_usd": 0.0633,
        },
        {
            "billable": "output_image",
            "unit": "image",
            "variant": "medium_1k",
            "cost_usd": 0.0633,
        },
        {
            "billable": "output_image",
            "unit": "image",
            "variant": "medium_2k",
            "cost_usd": 0.0844,
        },
    ]

    missing = client.get("/v1/images/models/openai/gpt-5.4/endpoints")
    assert missing.status_code == 200
    assert missing.json() == {"id": "openai/gpt-5.4", "endpoints": []}


def test_gateway_authorizes_and_settles_only_image_models(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "images"}).json()
    key_hash = created["data"]["hash"]

    rejected = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "openai/gpt-5.4-nano",
            "estimated_input_tokens": 16,
            "max_output_tokens": 1120,
            "route_type": "images",
            "idempotency_key": "image-text-model",
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["type"] == "model_not_supported"

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "google/gemini-3.1-flash-image",
            "estimated_input_tokens": 16,
            "max_output_tokens": 1120,
            "route_type": "images",
            "idempotency_key": "image-one",
            "request_fingerprint": "a" * 64,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    assert auth["model"] == "google/gemini-3.1-flash-image"
    assert auth["estimated_cost_microdollars"] > 0
    assert {candidate["model"] for candidate in auth["route_candidates"]} <= IMAGE_MODEL_ID_SET

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 16,
            "actual_output_tokens": 1120,
            "request_id": "img-request-one",
            "finish_reason": "stop",
            "route_type": "images",
            "selected_model": auth["model"],
            "selected_endpoint": auth["endpoint_id"],
            "elapsed_seconds": 1.5,
        },
    )
    assert settle.status_code == 200, settle.text
    result = settle.json()["data"]
    assert result["output_tokens"] == 1120
    assert result["cost_microdollars"] > 0
    generation = STORE.get_generation(result["generation_id"])
    assert generation is not None
    assert generation.route_type == "images"
    assert generation.tokens_completion == 1120


def test_fixed_price_image_reservation_and_settlement(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post(
        "/v1/keys", headers=user_headers, json={"name": "fixed-images"}
    ).json()
    key_hash = created["data"]["hash"]

    def authorize_fixed(idempotency_key: str) -> Response:
        return client.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": key_hash,
                "model": "x-ai/grok-imagine-image-2.0",
                "estimated_input_tokens": 8,
                "max_output_tokens": 1,
                "additional_cost_reservation_microdollars": 63_300,
                "route_type": "images",
                "idempotency_key": idempotency_key,
            },
        )

    authorize = authorize_fixed("fixed-image-one")
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    assert auth["usage_type"] == "Credits"
    assert auth["additional_cost_reservation_microdollars"] == 63_300
    assert auth["estimated_cost_microdollars"] == 63_300

    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "additional_cost_microdollars": 63_300,
            "request_id": "fixed-image-request-one",
            "finish_reason": "stop",
            "route_type": "images",
            "selected_model": auth["model"],
            "selected_endpoint": auth["endpoint_id"],
            "elapsed_seconds": 1.5,
        },
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["cost_microdollars"] == 63_300
    generation = STORE.get_generation(settled.json()["data"]["generation_id"])
    assert generation is not None
    assert generation.operator_cost_microdollars == 60_000

    mismatch_auth = authorize_fixed("fixed-image-mismatch").json()["data"]
    mismatched = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": mismatch_auth["authorization_id"],
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "additional_cost_microdollars": 0,
            "request_id": "fixed-image-request-mismatch",
            "finish_reason": "stop",
            "route_type": "images",
            "selected_model": mismatch_auth["model"],
            "selected_endpoint": mismatch_auth["endpoint_id"],
            "elapsed_seconds": 1.5,
        },
    )
    assert mismatched.status_code == 400
    assert "must match" in mismatched.json()["error"]["message"]

    unknown_quote = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "x-ai/grok-imagine-image-2.0",
            "estimated_input_tokens": 8,
            "max_output_tokens": 1,
            "additional_cost_reservation_microdollars": 63_301,
            "route_type": "images",
            "idempotency_key": "fixed-image-unknown-quote",
        },
    )
    assert unknown_quote.status_code == 400
    assert "unknown fixed-price image reservation" in unknown_quote.json()["error"][
        "message"
    ]

    rejected = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "openai/gpt-5.4-nano",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "additional_cost_reservation_microdollars": 1,
            "route_type": "chat.completions",
            "idempotency_key": "fixed-image-wrong-route",
        },
    )
    assert rejected.status_code == 400
