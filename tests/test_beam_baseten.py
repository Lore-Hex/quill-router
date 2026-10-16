from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.providers import baseten
from trusted_router import catalog_ingest
from trusted_router.catalog import MODELS, endpoints_for_model, model_to_openrouter_shape
from trusted_router.catalog_privacy import endpoint_e2ee, endpoint_zero_data_retention
from trusted_router.pricing import _customer_price

MODEL_ID = "reflection/beam-1.0"
NATIVE_ID = "reflection/Beam-1.0"

# Baseten's authenticated /v1/models row, checked 2026-10-16. The API prices
# resolve the output/cache labels transposed on its public library page.
BEAM_ROW = {
    "id": NATIVE_ID,
    "name": "Beam",
    "context_length": 1_048_576,
    "max_completion_tokens": 131_072,
    "pricing": {
        "prompt": "0.0000013",
        "completion": "0.0000043",
        "input_cache_read": "0.00000013",
    },
    "supported_features": ["tools", "json_mode", "structured_outputs", "reasoning"],
    "supported_sampling_parameters": ["temperature", "top_p", "stop", "max_tokens"],
    "input_modalities": ["text"],
    "output_modalities": ["text"],
}


def test_beam_is_discovered_without_a_handwritten_id_mapping(
    monkeypatch: pytest.MonkeyPatch, httpx_mock, tmp_path: Path,
) -> None:
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    monkeypatch.setattr(baseten, "EXPECTED_MODELS", [])
    monkeypatch.setattr(baseten, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(baseten, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(baseten, "MANIFEST_PATH", tmp_path / "baseten.json")
    httpx_mock.add_response(url=baseten.URL, json={"data": [BEAM_ROW]})

    result = baseten.fetch()
    assert NATIVE_ID not in baseten._NATIVE_TO_OR_ID
    assert baseten.UPSTREAM_ID_MAP[MODEL_ID] == NATIVE_ID
    assert httpx_mock.get_request().headers["Authorization"] == "Bearer test-key"
    price = result.prices[MODEL_ID]
    assert price.prompt_micro_per_m == 1_300_000
    assert price.completion_micro_per_m == 4_300_000
    assert price.tiers[0].prompt_cached_micro_per_m == 130_000

    baseten.write_provider_manifest(result)
    written = json.loads((tmp_path / "baseten.json").read_text())
    assert written["model_count"] == 1
    row = written["models"][0]
    assert row["id"] == MODEL_ID
    assert row["upstream_id"] == NATIVE_ID
    assert row["cached_input_token_price_per_m"] == 130_000
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert models[MODEL_ID].context_length == 1_048_576
    endpoint = endpoints[f"{MODEL_ID}@baseten/prepaid"]
    assert endpoint.upstream_id == NATIVE_ID
    assert endpoint.input_modalities == ("text",)
    assert {"tools", "response_format"}.issubset(endpoint.supported_parameters)
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == _customer_price(130_000)


def test_committed_beam_route_has_verified_pricing_and_privacy() -> None:
    model = MODELS[MODEL_ID]
    assert model.context_length == 1_048_576
    assert model.input_modalities == ("text",)
    assert model.output_modalities == ("text",)
    shape = model_to_openrouter_shape(model)
    assert shape["trustedrouter"]["open_weights"] is True
    endpoints = [ep for ep in endpoints_for_model(MODEL_ID) if ep.usage_type == "Credits"]
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint.provider == "baseten"
    assert endpoint.upstream_id == NATIVE_ID
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(1_300_000)
    assert endpoint.completion_price_microdollars_per_million_tokens == _customer_price(4_300_000)
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == _customer_price(130_000)
    assert endpoint_zero_data_retention(endpoint) is True
    assert endpoint_e2ee(endpoint) is not True
