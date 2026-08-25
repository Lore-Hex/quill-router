from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.catalog import endpoints_for_model
from trusted_router.catalog_registry import MODELS
from trusted_router.image_generation import IMAGE_MODEL_ID_SET
from trusted_router.storage import STORE


def test_image_catalog_is_machine_readable_and_matches_general_filter(
    client: TestClient,
) -> None:
    response = client.get("/v1/images/models")
    assert response.status_code == 200, response.text
    models = response.json()["data"]
    public_image_ids = {model["id"] for model in models}
    assert public_image_ids == {
        model_id for model_id in IMAGE_MODEL_ID_SET if endpoints_for_model(model_id)
    }
    for model in models:
        assert model["architecture"]["output_modalities"] == ["image"]
        assert model["supported_parameters"]["n"] == {
            "type": "range",
            "min": 1,
            "max": 1,
            "default": 1,
        }
        assert "output_format" not in model["supported_parameters"]
        assert "seed" not in model["supported_parameters"]
        assert model["supports_streaming"] is False
        assert model["endpoints"] == f"/v1/images/models/{model['id']}/endpoints"

    by_id = {model["id"]: model for model in models}
    gemini = by_id["google/gemini-3.1-flash-image"]
    assert gemini["supported_parameters"]["resolution"]["values"] == [
        "512",
        "1K",
        "2K",
        "4K",
    ]
    assert gemini["supported_parameters"]["input_references"] == {
        "type": "range",
        "min": 0,
        "max": 14,
    }
    assert by_id["recraft/recraftv4_1"]["architecture"]["input_modalities"] == ["text"]
    assert by_id["recraft/recraftv4_1"]["supported_parameters"]["resolution"] == {
        "type": "enum",
        "values": ["1K"],
        "default": "1K",
    }
    assert by_id["recraft/recraftv4_1_pro"]["supported_parameters"]["resolution"] == {
        "type": "enum",
        "values": ["2K"],
        "default": "2K",
    }
    decart = by_id["decart/lucy-image-2"]
    assert decart["architecture"]["input_modalities"] == ["text", "image"]
    assert decart["supported_parameters"]["input_references"] == {
        "type": "range",
        "min": 1,
        "max": 2,
    }
    assert decart["supported_parameters"]["resolution"] == {
        "type": "enum",
        "values": ["480p", "720p"],
        "default": "720p",
    }
    assert decart["pricing"]["image"] == "0.01055"
    assert decart["pricing"]["image_max"] == "0.0211"
    assert by_id["recraft/recraftv4_1"]["architecture"]["modality"] == "text->image"

    general = client.get("/v1/models")
    assert general.status_code == 200, general.text
    general_by_id = {model["id"]: model for model in general.json()["data"]}
    assert general_by_id["black-forest-labs/flux-2-klein-4b"]["pricing"]["image"] == "0.01477"

    filtered = client.get("/v1/models", params={"output_modalities": "image"})
    assert filtered.status_code == 200, filtered.text
    assert {model["id"] for model in filtered.json()["data"]} == public_image_ids
    assert MODELS["google/gemini-3.1-flash-image"].supports_chat is False


def test_image_endpoint_catalog_reports_resolution_prices(client: TestClient) -> None:
    response = client.get("/v1/images/models/google/gemini-3.1-flash-image/endpoints")
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
        row["billable"]: row["cost_usd"] for row in endpoint["pricing"] if row["unit"] == "token"
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

    missing = client.get("/v1/images/models/openai/gpt-5.4/endpoints")
    assert missing.status_code == 200
    assert missing.json() == {"id": "openai/gpt-5.4", "endpoints": []}

    fixed = client.get("/v1/images/models/black-forest-labs/flux-2-klein-4b/endpoints")
    assert fixed.status_code == 200, fixed.text
    fixed_endpoint = fixed.json()["endpoints"][0]
    assert fixed_endpoint["provider_slug"] == "bfl"
    assert fixed_endpoint["pricing"] == [
        {
            "billable": "output_image",
            "unit": "image",
            "variant": "1k",
            "cost_usd": 0.01477,
        }
    ]


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


def test_fixed_price_image_hold_settles_exactly_once(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "fixed-images"}).json()
    key_hash = created["data"]["hash"]
    quote = 14_770

    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "black-forest-labs/flux-2-klein-4b",
            "estimated_input_tokens": 1,
            "max_output_tokens": 1,
            "route_type": "images",
            "idempotency_key": "fixed-image-one",
            "request_fingerprint": "b" * 64,
            "additional_cost_reservation_microdollars": quote,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    assert auth["provider"] == "bfl"
    assert auth["additional_cost_reservation_microdollars"] == quote
    assert auth["estimated_cost_microdollars"] == quote

    settle_body = {
        "authorization_id": auth["authorization_id"],
        "actual_input_tokens": 0,
        "actual_output_tokens": 0,
        "additional_cost_microdollars": quote,
        "request_id": "fixed-image-request-one",
        "finish_reason": "stop",
        "route_type": "images",
        "selected_model": auth["model"],
        "selected_endpoint": auth["endpoint_id"],
        "elapsed_seconds": 1.5,
    }
    settled = client.post("/v1/internal/gateway/settle", json=settle_body)
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["cost_microdollars"] == quote

    replay = client.post("/v1/internal/gateway/settle", json=settle_body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["cost_microdollars"] == quote
