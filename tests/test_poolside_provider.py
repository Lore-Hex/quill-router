from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.base import ModelPrice, validate
from scripts.pricing.providers import poolside
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS, model_open_weights


def _model_row(
    model_id: str = "poolside/laguna-s-2.1",
    *,
    is_free: bool | None = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": model_id,
        "name": model_id.rsplit("/", 1)[-1],
        "context_length": 262_144,
        "max_completion_tokens": 32_768,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "supported_features": ["tools", "reasoning"],
        "supported_sampling_parameters": ["temperature"],
        "pricing": {
            "prompt": "0",
            "completion": "0",
            "input_cache_read": "0",
        },
    }
    if is_free is not None:
        row["is_free"] = is_free
    return row


def test_poolside_accepts_only_explicit_authoritative_free_prices() -> None:
    prices, discovered = poolside._parse_catalog(  # noqa: SLF001
        {"data": [_model_row()]}
    )

    assert prices == {
        "poolside/laguna-s-2.1": ModelPrice(
            prompt_micro_per_m=0,
            completion_micro_per_m=0,
            prompt_cached_micro_per_m=0,
        )
    }
    assert discovered["poolside/laguna-s-2.1"]["supported_features"] == [
        "chat",
        "completion",
        "reasoning",
        "tools",
        "prompt_caching",
    ]


@pytest.mark.parametrize("is_free", [False, None])
def test_poolside_rejects_ambiguous_zero_prices(is_free: bool | None) -> None:
    with pytest.raises(RuntimeError, match="without is_free=true"):
        poolside._parse_catalog(  # noqa: SLF001
            {"data": [_model_row(is_free=is_free)]}
        )


def test_global_zero_price_guard_remains_fail_closed() -> None:
    prices = {
        "poolside/laguna-s-2.1": ModelPrice(
            prompt_micro_per_m=0,
            completion_micro_per_m=0,
        )
    }

    assert "all prices are zero" in validate(prices, [])[0]
    assert validate(prices, [], allow_authoritative_all_zero=True) == []


def test_poolside_fetch_requires_visible_exact_pong_canaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_model_row(model_id) for model_id in poolside.EXPECTED_MODELS]
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": rows}

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    def fake_probe(**kwargs: object) -> bool:
        calls.append(kwargs)
        return kwargs["model"] == "poolside/laguna-s-2.1"

    monkeypatch.setenv("POOLSIDE_API_KEY", "test-key")
    monkeypatch.setattr(poolside, "MANIFEST_PATH", tmp_path / "poolside.json")
    monkeypatch.setattr(poolside.httpx, "Client", FakeClient)
    monkeypatch.setattr(poolside, "probe_openai_chat", fake_probe)

    result = poolside.fetch()

    assert set(result.prices) == set(poolside.EXPECTED_MODELS)
    assert len(calls) == 2
    assert all(call["max_tokens"] == 512 for call in calls)
    assert all(call["expected_content"] == "PONG" for call in calls)
    assert all(
        call["prompt"] == "Reply with exactly PONG and nothing else."
        for call in calls
    )
    assert poolside._DISCOVERED_MANIFEST_ROWS[  # noqa: SLF001
        "poolside/laguna-s-2.1"
    ]["routable"] is True
    assert poolside._DISCOVERED_MANIFEST_ROWS[  # noqa: SLF001
        "poolside/laguna-xs-2.1"
    ]["routable"] is False


def test_poolside_catalog_is_prepaid_open_weight_and_standard_privacy() -> None:
    provider = PROVIDERS["poolside"]
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False

    endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "poolside"
    ]
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits"}
    assert {endpoint.prompt_price_microdollars_per_million_tokens for endpoint in endpoints} == {
        10_000
    }
    assert {endpoint.completion_price_microdollars_per_million_tokens for endpoint in endpoints} == {
        10_000
    }
    assert all(model_open_weights(MODELS[model_id]) for model_id in poolside.EXPECTED_MODELS)


def test_poolside_hourly_refresh_and_secret_wiring_are_complete() -> None:
    discoverable = {
        slug: (url, env_names)
        for slug, url, env_names, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
    }
    assert discoverable["poolside"] == (
        "https://inference.poolside.ai/v1/models",
        ("POOLSIDE_API_KEY",),
    )

    root = Path(__file__).resolve().parents[1]
    secrets = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'ensure_secret_from_env_file "POOLSIDE_API_KEY" '
        '"trustedrouter-poolside-api-key"'
    ) in secrets
    assert 'grant_tr_deploy_secret_access "trustedrouter-poolside-api-key"' in secrets
    assert "POOLSIDE_API_KEY:trustedrouter-poolside-api-key" in workflow


def test_poolside_manifest_matches_live_contract_shape() -> None:
    raw = json.loads(poolside.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert raw["provider"] == "poolside"
    assert raw["source"] == "https://inference.poolside.ai/v1/models"
    assert raw["model_count"] == 2
    assert {row["id"] for row in raw["models"]} == set(poolside.EXPECTED_MODELS)
    assert all(row["context_length"] == 262_144 for row in raw["models"])
    assert all(row["max_output_tokens"] == 32_768 for row in raw["models"])
    assert all(row["routable"] is True for row in raw["models"])
