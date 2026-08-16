from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.pricing.providers import gmi
from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.catalog_data import PRIVACY_TIER_STANDARD
from trusted_router.catalog_privacy import (
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoint_privacy_tier,
    endpoint_stores_content,
    endpoint_zero_data_retention,
)

KIMI_K3 = "moonshotai/kimi-k3"


def test_gmi_hourly_parser_discovers_kimi_k3_exact_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_payload = {
        "data": [
            {
                "id": KIMI_K3,
            }
        ]
    }
    price_payload = {
        "modelPrices": [
            {
                "billingType": "llm",
                "modelName": KIMI_K3,
                "pricePer1mPromptToken": 3_000_000,
                "pricePer1mCompletionToken": 15_000_000,
                "tiers": [
                    {
                        "threshold": 0,
                        "inputPrice": 3_000_000,
                        "outputPrice": 15_000_000,
                        "cacheReadPrice": 300_000,
                    }
                ],
            }
        ]
    }

    def fake_fetch_json(url: str, **_kwargs: object) -> object:
        return price_payload if url == gmi.PRICE_URL else catalog_payload

    monkeypatch.setenv("GMI_API_KEY", "test-gmi-key")
    monkeypatch.setattr(gmi, "fetch_json", fake_fetch_json)

    result = gmi.fetch()

    assert result.prices[KIMI_K3].prompt_micro_per_m == 3_000_000
    assert result.prices[KIMI_K3].completion_micro_per_m == 15_000_000
    assert result.prices[KIMI_K3].tiers[0].prompt_cached_micro_per_m == 300_000
    assert gmi.UPSTREAM_ID_MAP[KIMI_K3] == KIMI_K3


def test_gmi_recovers_live_model_omitted_from_authenticated_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "gmi.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "gmi",
                "models": [
                    {
                        "id": "z-ai/glm-5.2",
                        "upstream_id": "zai-org/GLM-5.2-FP8",
                        "display_name": "GMI Cloud GLM 5.2",
                        "model_type": "chat",
                        "context_length": 1_048_576,
                        "max_output_tokens": 131_072,
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "endpoints": ["chat/completions"],
                        "status": 1,
                        "input_token_price_per_m": 980_000,
                        "output_token_price_per_m": 3_080_000,
                        "cached_input_token_price_per_m": 182_000,
                        "missing_since": "2026-08-13",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    price_payload = {
        "modelPrices": [
            {
                "billingType": "llm",
                "modelName": "zai-org/GLM-5.2-FP8",
                "pricePer1mPromptToken": 742_000,
                "pricePer1mCompletionToken": 2_332_000,
                "tiers": [
                    {
                        "threshold": 0,
                        "inputPrice": 742_000,
                        "outputPrice": 2_332_000,
                        "cacheReadPrice": 137_800,
                    }
                ],
            }
        ]
    }
    canary_calls: list[dict[str, object]] = []

    def fake_fetch_json(url: str, **_kwargs: object) -> object:
        return price_payload if url == gmi.PRICE_URL else {"data": []}

    def fake_probe(**kwargs: object) -> bool:
        canary_calls.append(kwargs)
        return True

    monkeypatch.setenv("GMI_API_KEY", "test-gmi-key")
    monkeypatch.setattr(gmi, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(gmi, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(gmi, "probe_openai_chat", fake_probe)

    result = gmi.fetch()
    gmi.write_provider_manifest(result)

    assert result.prices["z-ai/glm-5.2"].prompt_micro_per_m == 742_000
    assert result.prices["z-ai/glm-5.2"].completion_micro_per_m == 2_332_000
    assert canary_calls == [
        {
            "base_url": gmi.BASE_URL,
            "api_key": "test-gmi-key",
            "model": "zai-org/GLM-5.2-FP8",
            "expected_content": "PONG",
            "max_tokens": 256,
        }
    ]
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = next(item for item in written["models"] if item["id"] == "z-ai/glm-5.2")
    assert row["input_token_price_per_m"] == 742_000
    assert row["output_token_price_per_m"] == 2_332_000
    assert row["cached_input_token_price_per_m"] == 137_800
    assert "missing_since" not in row


def test_gmi_does_not_recover_an_omitted_model_when_canary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price_payload = {
        "modelPrices": [
            {
                "billingType": "llm",
                "modelName": "zai-org/GLM-5.2-FP8",
                "pricePer1mPromptToken": 742_000,
                "pricePer1mCompletionToken": 2_332_000,
            }
        ]
    }

    def fake_fetch_json(url: str, **_kwargs: object) -> object:
        return price_payload if url == gmi.PRICE_URL else {"data": []}

    monkeypatch.setenv("GMI_API_KEY", "test-gmi-key")
    monkeypatch.setattr(gmi, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(gmi, "probe_openai_chat", lambda **_kwargs: False)

    result = gmi.fetch()

    assert "z-ai/glm-5.2" not in result.prices
    assert "z-ai/glm-5.2" not in gmi._DISCOVERED_MANIFEST_ROWS


def test_gmi_kimi_k3_is_a_verified_prepaid_route() -> None:
    endpoint = MODEL_ENDPOINTS[f"{KIMI_K3}@gmi/prepaid"]

    assert endpoint.upstream_id == "moonshotai/kimi-k3"
    assert endpoint.prompt_price_microdollars_per_million_tokens == 3_165_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 15_825_000


def test_phala_kimi_k3_pass_through_is_standard_not_confidential() -> None:
    endpoint = MODEL_ENDPOINTS[f"{KIMI_K3}@phala/prepaid"]

    assert endpoint.upstream_id == "moonshotai/kimi-k3"
    assert endpoint_privacy_tier(endpoint) == PRIVACY_TIER_STANDARD
    assert endpoint_stores_content(endpoint) is True
    assert endpoint_zero_data_retention(endpoint) is False
    assert endpoint_confidential_compute(endpoint) is False
    assert endpoint_e2ee(endpoint) is False


def test_kimi_k3_public_catalog_reports_route_specific_phala_posture(
    client: TestClient,
) -> None:
    response = client.get("/v1/models/moonshotai/kimi-k3/endpoints")

    assert response.status_code == 200
    phala = next(
        row
        for row in response.json()["data"]
        if row["provider"] == "phala" and row["usage_type"] == "Credits"
    )
    assert phala["upstream_id"] == "moonshotai/kimi-k3"
    assert phala["trustedrouter"]["privacy_tier"] == PRIVACY_TIER_STANDARD
    assert phala["trustedrouter"]["stores_content"] is True
    assert phala["trustedrouter"]["provider_zero_data_retention"] is False
    assert phala["trustedrouter"]["provider_confidential_compute"] is False
    assert phala["trustedrouter"]["provider_e2ee"] is False
    assert "pass-through" in phala["trustedrouter"]["provider_policy"].casefold()
