from __future__ import annotations

import html
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.pricing import base
from scripts.pricing.parsers import openai as parser
from scripts.pricing.providers import openai
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.image_generation import OPENAI_IMAGE_MODEL_IDS
from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars
from trusted_router.storage import STORE

IMAGE_PRICES = """Image generation models
Standard
| gpt-image-2.5-flare | Image | $8 | $2 | $30 |
| gpt-image-2.5-flare | Text | $5 | $1.25 | - |
| gpt-image-2.5-sunburst | Image | $8 | $2 | $30 |
| gpt-image-2.5-sunburst | Text | $5 | $1.25 | - |
Batch
| gpt-image-2.5-flare | Image | $4 | $1 | $15 |
| gpt-image-2.5-flare | Text | $2.5 | $0.625 | - |
"""


def test_image_text_input_and_image_output_prices_are_not_conflated() -> None:
    prices = parser.parse(IMAGE_PRICES)
    for model in OPENAI_IMAGE_MODEL_IDS:
        assert prices[model] == {
            "prompt_micro_per_m": 5_000_000,
            "completion_micro_per_m": 30_000_000,
            "prompt_cached_micro_per_m": 1_250_000,
        }
    assert "openai/gpt-image-2.5-flare" not in parser.parse(
        "| gpt-image-2.5-flare | Image | $8 | $2 | $30 |"
    )


def test_hidden_standard_group_beats_rendered_batch_projection() -> None:
    # Actual GroupedPricingTable serialization used by the official page.
    props = {"groups": [1, [[0, {
        "model": [0, "gpt-image-2.5-flare"],
        "rows": [1, [[1, [[0, "Image"], [0, 8], [0, 2], [0, 30]]],
                     [1, [[0, "Text"], [0, 5], [0, 1.25], [0, "-"]]]]],
    }]]]}
    island = '<astro-island component-export="GroupedPricingTable" props="' + html.escape(json.dumps(props), quote=True) + '"></astro-island>'
    source = IMAGE_PRICES.split("Batch\n")[1] + '<div data-content-switcher-pane="true" data-value="standard">' + island + "</div>"
    assert parser.parse(source)["openai/gpt-image-2.5-flare"]["completion_micro_per_m"] == 30_000_000
    batch_only = '<div data-content-switcher-pane="true" data-value="batch">' + island + "</div>"
    assert "openai/gpt-image-2.5-flare" not in parser.parse(batch_only)


@pytest.mark.parametrize("healthy", [True, False])
def test_image_discovery_uses_native_image_canary_not_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: bool,
) -> None:
    manifest = tmp_path / "openai.json"
    manifest.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(openai, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(openai, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(openai, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(base, "fetch_html", lambda *_a, **_k: IMAGE_PRICES)
    monkeypatch.setattr(openai, "fetch_json", lambda *_a, **_k: {"data": [
        {"id": "gpt-4.1"}, {"id": "gpt-image-2.5-flare"}, {"id": "gpt-image-2.5-sunburst"},
        {"id": "gpt-image-99-unknown"},
    ]})
    chat_calls: list[str] = []
    image_calls: list[str] = []

    def chat(**kwargs: str) -> bool:
        chat_calls.append(kwargs["model"])
        return True

    def image(**kwargs: str) -> bool:
        image_calls.append(kwargs["model"])
        return healthy

    monkeypatch.setattr(openai, "probe_openai_chat", chat)
    monkeypatch.setattr(openai, "probe_openai_image", image)
    openai.write_provider_manifest(openai.fetch())
    rows = {row["id"]: row for row in json.loads(manifest.read_text())["models"]}
    assert chat_calls == ["gpt-4.1"]
    assert set(image_calls) == {model.removeprefix("openai/") for model in OPENAI_IMAGE_MODEL_IDS}
    for model in OPENAI_IMAGE_MODEL_IDS:
        assert rows[model]["model_type"] == "image"
        assert rows[model]["endpoints"] == ["images"]
        assert rows[model].get("routable", True) is healthy
    assert "openai/gpt-image-99-unknown" not in rows


@pytest.mark.parametrize("model", sorted(OPENAI_IMAGE_MODEL_IDS))
def test_image_catalog_and_exact_cached_billing(client: TestClient, model: str) -> None:
    endpoint = MODEL_ENDPOINTS[f"{model}@openai/prepaid"]
    assert not MODELS[model].supports_chat
    assert endpoint.prompt_price_microdollars_per_million_tokens == 5_275_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 31_650_000
    # Actual counters only, with cached reads charged once at their own rate.
    assert _endpoint_cost_microdollars(endpoint, 1_000_000, 1_000_000, cache_read_tokens=1_000_000) == 38_243_750
    response = client.get(f"/v1/images/models/{model}/endpoints")
    assert response.status_code == 200
    row = next(row for row in response.json()["endpoints"] if row["trustedrouter"]["usage_type"] == "Credits")
    assert all(price["unit"] == "token" for price in row["pricing"])
    assert {price["billable"] for price in row["pricing"]} == {"input_text", "input_text_cache_read", "output_image"}
    assert row["supported_parameters"]["input_references"]["max"] == 0
    assert row["supported_parameters"]["aspect_ratio"]["default"] == "auto"
    assert row["allowed_passthrough_parameters"] == ["moderation"]
    assert row["supports_streaming"] is False  # completion-only, not native partial renders


def test_image_cached_settlement_is_exactly_once(
    client: TestClient, user_headers: dict[str, str],
) -> None:
    key = client.post("/v1/keys", headers=user_headers, json={"name": "image-25"}).json()["data"]["hash"]
    authorized = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key, "model": "openai/gpt-image-2.5-flare",
        "estimated_input_tokens": 1000, "max_output_tokens": 32768,
        "route_type": "images", "idempotency_key": "image-25-cached",
        "request_fingerprint": "a" * 64,
    })
    assert authorized.status_code == 200, authorized.text
    auth = authorized.json()["data"]
    request = {
        "authorization_id": auth["authorization_id"],
        "actual_input_tokens": 1000, "cache_read_input_tokens": 800,
        "actual_output_tokens": 196, "request_id": "image-25-cached",
        "finish_reason": "stop", "route_type": "images",
        "selected_model": auth["model"], "selected_endpoint": auth["endpoint_id"],
        "elapsed_seconds": 1.5,
    }
    first = client.post("/v1/internal/gateway/settle", json=request)
    second = client.post("/v1/internal/gateway/settle", json=request)
    assert first.status_code == second.status_code == 200
    result = first.json()["data"]
    assert result["cost_microdollars"] == 8313  # 200 uncached + 800 cached + 196 image output.
    assert second.json()["data"]["generation_id"] == result["generation_id"]
    generation = STORE.get_generation(result["generation_id"])
    assert generation is not None
    assert generation.tokens_prompt == 1000
    assert generation.tokens_completion == 196
