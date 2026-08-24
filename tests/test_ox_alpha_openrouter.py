from __future__ import annotations

import json

import pytest

from scripts.pricing.providers import openrouter_exclusive
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS, endpoints_for_model
from trusted_router.catalog_data import PRIVACY_TIER_STANDARD
from trusted_router.catalog_privacy import endpoint_privacy_tier
from trusted_router.pricing import _PRICE_FLOOR_MICRODOLLARS_PER_M

MODEL_ID = "stealth/ox-alpha"
PROVIDER_SLUG = "openrouter-exclusive"


def _endpoint_payload(*, model_id: str = MODEL_ID, status: int = 0) -> dict[str, object]:
    return {
        "data": {
            "endpoints": [
                {
                    "provider_name": "Stealth",
                    "model_id": model_id,
                    "status": status,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        }
    }


def test_ox_alpha_is_one_credits_only_standard_privacy_route() -> None:
    model = MODELS[MODEL_ID]
    assert model.name == "Ox Alpha"
    assert model.context_length == 1_048_576
    assert model.input_modalities == ("text", "image")
    assert model.output_modalities == ("text",)

    endpoints = endpoints_for_model(MODEL_ID)
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint.id == f"{MODEL_ID}@{PROVIDER_SLUG}/prepaid"
    assert endpoint.provider == PROVIDER_SLUG
    assert endpoint.usage_type == "Credits"
    assert endpoint.upstream_id == MODEL_ID
    assert endpoint.prompt_price_microdollars_per_million_tokens == (
        _PRICE_FLOOR_MICRODOLLARS_PER_M
    )
    assert endpoint.completion_price_microdollars_per_million_tokens == (
        _PRICE_FLOOR_MICRODOLLARS_PER_M
    )
    assert endpoint_privacy_tier(endpoint) == PRIVACY_TIER_STANDARD

    provider = PROVIDERS[PROVIDER_SLUG]
    assert provider.name == "Stealth via OpenRouter"
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert "retains prompts and completions" in provider.provider_policy
    assert provider.provider_policy_url == "https://openrouter.ai/terms/stealth"


def test_openrouter_exclusive_identity_cannot_expose_another_model() -> None:
    exclusive = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == PROVIDER_SLUG
    ]
    assert [(endpoint.model_id, endpoint.upstream_id) for endpoint in exclusive] == [
        (MODEL_ID, MODEL_ID)
    ]


def test_ox_alpha_price_refresh_accepts_exact_healthy_free_preview(monkeypatch) -> None:
    monkeypatch.setattr(openrouter_exclusive, "fetch_json", lambda _url: _endpoint_payload())
    result = openrouter_exclusive.fetch()

    assert result.slug == PROVIDER_SLUG
    assert set(result.prices) == {MODEL_ID}
    price = result.prices[MODEL_ID]
    assert price.prompt_micro_per_m == 0
    assert price.completion_micro_per_m == 0


@pytest.mark.parametrize(
    ("model_id", "status"),
    [("openai/gpt-5.5", 0), (MODEL_ID, 1)],
)
def test_ox_alpha_price_refresh_rejects_non_allowlisted_or_unhealthy_endpoint(
    monkeypatch,
    model_id: str,
    status: int,
) -> None:
    monkeypatch.setattr(
        openrouter_exclusive,
        "fetch_json",
        lambda _url: _endpoint_payload(model_id=model_id, status=status),
    )
    with pytest.raises(RuntimeError, match="expected one healthy Stealth Ox Alpha endpoint"):
        openrouter_exclusive.fetch()


def test_ox_alpha_manifest_writer_cannot_change_the_allowlisted_model(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "openrouter-exclusive.json"
    source = openrouter_exclusive.MANIFEST_PATH
    path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(openrouter_exclusive, "MANIFEST_PATH", path)
    monkeypatch.setattr(openrouter_exclusive, "fetch_json", lambda _url: _endpoint_payload())

    notes = openrouter_exclusive.write_provider_manifest(openrouter_exclusive.fetch())
    written = json.loads(path.read_text(encoding="utf-8"))

    assert notes == ["openrouter-exclusive: refreshed Ox Alpha pricing from its exact endpoint"]
    assert written["model_count"] == 1
    assert [row["id"] for row in written["models"]] == [MODEL_ID]
    assert [row["upstream_id"] for row in written["models"]] == [MODEL_ID]
