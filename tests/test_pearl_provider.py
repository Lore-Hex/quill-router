from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.providers import pearl
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS


def _model_row(
    native_id: str,
    *,
    prompt: str,
    completion: str,
    cached: str,
    context_length: int = 1_048_576,
    input_modalities: list[str] | None = None,
    features: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": native_id,
        "name": native_id,
        "context_length": context_length,
        "max_output_tokens": context_length,
        "input_modalities": input_modalities or ["text"],
        "output_modalities": ["text"],
        "supported_features": features
        or ["tools", "json_mode", "reasoning"],
        "supported_sampling_parameters": ["temperature", "max_tokens"],
        "pricing": {
            "prompt": prompt,
            "completion": completion,
            "input_cache_read": cached,
        },
    }


def _payload() -> dict[str, Any]:
    return {
        "data": [
            _model_row(
                "google/gemma-4-31b-it",
                prompt="0.000000120000000",
                completion="0.000000360000000",
                cached="0.000000090000000",
                context_length=262_144,
                input_modalities=["text", "image"],
                features=["tools", "json_mode"],
            ),
            _model_row(
                "zai-org/GLM-5.2",
                prompt="0.000001200000000",
                completion="0.000004100000000",
                cached="0.000000200000000",
                context_length=1_000_000,
            ),
            _model_row(
                "deepseek-ai/DeepSeek-V4-Pro",
                prompt="0.000001300000000",
                completion="0.000002600000000",
                cached="0.000000100000000",
            ),
            _model_row(
                "deepseek/deepseek-v4-flash-0731",
                prompt="0.000000140000000",
                completion="0.000000280000000",
                cached="0.000000028000000",
            ),
            _model_row(
                "deepseek-ai/DeepSeek-V4-Flash",
                prompt="0.000000110000000",
                completion="0.000000200000000",
                cached="0.000000020000000",
            ),
        ],
        "pricing_source": "provider",
    }


def test_pearl_parser_preserves_native_ids_capabilities_and_exact_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pearl, "UPSTREAM_ID_MAP", {})
    prices, discovered = pearl._parse_catalog(_payload())

    assert set(prices) == set(pearl.EXPECTED_MODELS)
    assert prices["google/gemma-4-31b-it"].prompt_micro_per_m == 120_000
    assert prices["google/gemma-4-31b-it"].completion_micro_per_m == 360_000
    assert (
        prices["google/gemma-4-31b-it"].tiers[0].prompt_cached_micro_per_m
        == 90_000
    )
    assert prices["z-ai/glm-5.2"].completion_micro_per_m == 4_100_000
    assert prices["deepseek/deepseek-v4-flash-0731"].prompt_micro_per_m == 140_000
    assert pearl.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai-org/GLM-5.2"
    assert (
        pearl.UPSTREAM_ID_MAP["deepseek/deepseek-v4-pro"]
        == "deepseek-ai/DeepSeek-V4-Pro"
    )
    gemma = discovered["google/gemma-4-31b-it"]
    assert gemma["input_modalities"] == ["text", "image"]
    assert "structured_outputs" in gemma["supported_features"]
    assert "prompt_caching" in gemma["supported_features"]
    assert "reasoning" not in gemma["supported_features"]


def test_pearl_fetch_discovers_models_and_fails_one_bad_canary_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return copy.deepcopy(payload)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    checked = frozenset(pearl.EXPECTED_MODELS)
    canaried: list[str] = []

    def probe(**kwargs: object) -> bool:
        assert kwargs["expected_content"] == "PONG"
        assert kwargs["max_tokens"] == 256
        model = str(kwargs["model"])
        canaried.append(model)
        return model != "zai-org/GLM-5.2"

    monkeypatch.setenv("PEARL_RESEARCH_API_KEY", "test-key")
    monkeypatch.setattr(pearl.httpx, "Client", FakeClient)
    monkeypatch.setattr(pearl, "models_requiring_canary", lambda *_args: checked)
    monkeypatch.setattr(pearl, "probe_openai_chat", probe)

    result = pearl.fetch()

    assert set(result.prices) == set(pearl.EXPECTED_MODELS)
    assert set(canaried) == set(pearl.UPSTREAM_ID_MAP.values())
    assert pearl._DISCOVERED_MANIFEST_ROWS["z-ai/glm-5.2"]["routable"] is False
    assert (
        pearl._DISCOVERED_MANIFEST_ROWS["z-ai/glm-5.2"]["routable_reason"]
        == "provider-canary-failed"
    )
    assert (
        pearl._DISCOVERED_MANIFEST_ROWS["deepseek/deepseek-v4-pro"]["routable"]
        is True
    )


def test_pearl_catalog_is_prepaid_only_and_standard_privacy() -> None:
    provider = PROVIDERS["pearl"]
    assert provider.name == "Pearl Research Labs"
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert provider.provider_policy_url == "https://pearlresearch.ai/legal/privacy"
    assert provider.provider_headquarters_country == "IL"

    endpoints = [
        endpoint
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.provider == "pearl"
    ]
    assert endpoints
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits"}
    assert all(endpoint.id.endswith("@pearl/prepaid") for endpoint in endpoints)


def test_pearl_public_api_exposes_provider_and_exact_endpoint(client: Any) -> None:
    providers = {
        row["id"]: row for row in client.get("/v1/providers").json()["data"]
    }
    provider = providers["pearl"]
    assert provider["name"] == "Pearl Research Labs"
    assert provider["supports_prepaid"] is True
    assert provider["supports_byok"] is False
    assert provider["stores_content"] is True
    assert provider["provider_zero_data_retention"] is False
    assert provider["provider_confidential_compute"] is False
    assert provider["provider_e2ee"] is False

    response = client.get("/v1/models/z-ai/glm-5.2/endpoints")
    assert response.status_code == 200
    endpoint = next(
        row
        for row in response.json()["data"]
        if row["provider_name"] == "Pearl Research Labs"
    )
    assert endpoint["upstream_id"] == "zai-org/GLM-5.2"
    assert endpoint["pricing"]["prompt"] == "0.000001266"
    assert endpoint["pricing"]["completion"] == "0.0000043255"


def test_pearl_hourly_refresh_and_secret_wiring_are_complete() -> None:
    discoverable = {
        slug: (url, env_names)
        for slug, url, env_names, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
    }
    assert discoverable["pearl"] == (
        "https://inference.pearlresearch.ai/v1/models",
        ("PEARL_RESEARCH_API_KEY",),
    )

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'ensure_secret_from_env_file "PEARL_RESEARCH_API_KEY" '
        '"trustedrouter-pearl-api-key"'
    ) in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-pearl-api-key"' in secrets
    assert "PEARL_RESEARCH_API_KEY:trustedrouter-pearl-api-key" in workflow
