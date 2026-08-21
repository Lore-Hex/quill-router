from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from pytest import MonkeyPatch

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import cerebras
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model

_CUTOFF = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
_GEMMA = "google/gemma-4-31b-it"
_GEMMA_ALIAS = "cerebras/gemma-4-31b"
_GEMMA_UPSTREAM = "gemma-4-31b"
_QWEN = "qwen/qwen3.8-27b"


class _FakeCerebrasResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "zai-glm-4.7",
                    "name": "Z.ai GLM 4.7",
                    "pricing": {
                        "prompt": "0.00000225",
                        "completion": "0.00000275",
                    },
                    "capabilities": {"reasoning": True, "vision": False},
                    "limits": {
                        "max_context_length": 131_072,
                        "max_completion_tokens": 40_960,
                    },
                    "deprecated": False,
                },
                {
                    "id": "gemma-4-31b",
                    "name": "Gemma 4 31B",
                    "pricing": {
                        "prompt": "0.00000099",
                        "completion": "0.00000149",
                    },
                    "capabilities": {
                        "function_calling": True,
                        "reasoning": True,
                        "structured_outputs": True,
                        "vision": True,
                    },
                    "limits": {
                        "max_context_length": 131_072,
                        "max_completion_tokens": 40_960,
                    },
                    "deprecated": False,
                },
                {
                    "id": "gpt-oss-120b",
                    "name": "OpenAI GPT OSS",
                    "pricing": {
                        "prompt": "0.00000035",
                        "completion": "0.00000075",
                    },
                    "capabilities": {"reasoning": True, "vision": False},
                    "limits": {
                        "max_context_length": 131_072,
                        "max_completion_tokens": 40_960,
                    },
                    "deprecated": False,
                },
                {
                    "id": "retired-model",
                    "pricing": {
                        "prompt": "0.00000001",
                        "completion": "0.00000001",
                    },
                    "deprecated": True,
                },
            ]
        }


class _FakeCerebrasClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeCerebrasClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str]) -> _FakeCerebrasResponse:
        assert url == cerebras.URL
        assert headers["Accept"] == "application/json"
        assert "Authorization" not in headers
        return _FakeCerebrasResponse()


def test_cerebras_public_api_discovers_models_prices_and_capabilities(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cerebras.httpx, "Client", _FakeCerebrasClient)
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )

    result = cerebras.fetch()

    gemma = result.prices["google/gemma-4-31b-it"]
    assert gemma.prompt_micro_per_m == 990_000
    assert gemma.completion_micro_per_m == 1_490_000
    assert result.prices["cerebras/gemma-4-31b"] == gemma
    assert result.prices["openai/gpt-oss-120b"].prompt_micro_per_m == 350_000
    assert result.prices["z-ai/glm-4.7"].completion_micro_per_m == 2_750_000

    row = cerebras._DISCOVERED_MANIFEST_ROWS["google/gemma-4-31b-it"]
    assert row["upstream_id"] == "gemma-4-31b"
    assert row["input_modalities"] == ["text", "image"]
    assert row["context_length"] == 131_072
    assert row["max_output_tokens"] == 40_960
    assert "function-calling" in row["features"]
    assert "structured-outputs" in row["features"]


def test_cerebras_refresh_writes_new_models_and_aliases(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cerebras.httpx, "Client", _FakeCerebrasClient)
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    manifest_path = tmp_path / "cerebras.json"
    manifest_path.write_text(
        json.dumps({"provider": "cerebras", "models": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cerebras, "MANIFEST_PATH", manifest_path)

    result = cerebras.fetch()
    cerebras.write_provider_manifest(result)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in payload["models"]}
    assert payload["source"] == cerebras.URL
    assert payload["model_count"] == 6
    assert {
        "openai/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        "z-ai/glm-4.7",
        "cerebras/zai-glm-4.7",
        "google/gemma-4-31b-it",
        "cerebras/gemma-4-31b",
    } == set(rows)
    assert rows["google/gemma-4-31b-it"]["input_token_price_per_m"] == 990_000
    assert rows["google/gemma-4-31b-it"]["upstream_id"] == "gemma-4-31b"


def test_cerebras_qwen38_is_discovered_only_from_a_live_priced_feed(
    monkeypatch: MonkeyPatch,
) -> None:
    payload = _FakeCerebrasResponse().json()
    payload["data"].append(
        {
            "id": "qwen-3.8-27b",
            "name": "Qwen 3.8 27B",
            "pricing": {
                "prompt": "0.00000040",
                "completion": "0.00000120",
            },
            "capabilities": {
                "function_calling": True,
                "structured_outputs": True,
                "vision": True,
            },
            "limits": {
                "max_context_length": 131_072,
                "max_completion_tokens": 32_768,
            },
            "deprecated": False,
        }
    )
    monkeypatch.setattr(_FakeCerebrasResponse, "json", lambda _self: payload)
    monkeypatch.setattr(cerebras.httpx, "Client", _FakeCerebrasClient)

    result = cerebras.fetch()

    assert result.prices[_QWEN] == ModelPrice(400_000, 1_200_000)
    assert result.prices["cerebras/qwen-3.8-27b"] == result.prices[_QWEN]
    assert cerebras.UPSTREAM_ID_MAP[_QWEN] == "qwen-3.8-27b"
    row = cerebras._DISCOVERED_MANIFEST_ROWS[_QWEN]
    assert row["input_modalities"] == ["text", "image"]
    assert row["upstream_id"] == "qwen-3.8-27b"


def test_cerebras_gemma_shared_route_retires_at_announced_cutover() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "cerebras",
        _GEMMA,
        _GEMMA_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "cerebras",
        _GEMMA,
        _GEMMA_UPSTREAM,
        at=_CUTOFF,
    )
    assert provider_lifecycle.provider_model_retired(
        "cerebras",
        _GEMMA_ALIAS,
        _GEMMA_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "another-provider",
        _GEMMA,
        _GEMMA_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "cerebras",
        _QWEN,
        "qwen-3.8-27b",
        at=_CUTOFF,
    )


def test_cerebras_parser_filters_retired_gemma_from_a_stale_feed(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cerebras.httpx, "Client", _FakeCerebrasClient)
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = cerebras.fetch()
    assert _GEMMA in before.prices
    assert _GEMMA_ALIAS in before.prices

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = cerebras.fetch()
    assert _GEMMA not in after.prices
    assert _GEMMA_ALIAS not in after.prices
    assert _GEMMA not in cerebras._DISCOVERED_MANIFEST_ROWS
    assert "openai/gpt-oss-120b" in after.prices


def test_hourly_refresh_cannot_restore_retired_cerebras_gemma(
    monkeypatch: MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="cerebras",
        prices={
            _GEMMA: ModelPrice(990_000, 1_490_000),
            _QWEN: ModelPrice(400_000, 1_200_000),
        },
        source="api",
        fetched_url=cerebras.URL,
    )
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"cerebras": result})
    assert "cerebras" in before[_GEMMA]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"cerebras": result})
    assert _GEMMA not in after
    assert "cerebras" in after[_QWEN]


def test_cerebras_gemma_retirement_is_visible_and_provider_scoped(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)

    providers = {endpoint.provider for endpoint in endpoints_for_model(_GEMMA)}

    assert "cerebras" not in providers
    assert providers


def test_cerebras_manifest_records_shared_tier_replacement() -> None:
    rows = {
        row["id"]: row
        for row in json.loads(cerebras.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    }
    for model_id in (_GEMMA, _GEMMA_ALIAS):
        assert rows[model_id]["retirement_at"] == "2026-09-03T00:00:00Z"
        assert rows[model_id]["replacement_model_id"] == _QWEN


def test_cerebras_feed_gate_uses_a_non_retiring_anchor() -> None:
    assert cerebras.EXPECTED_MODELS == ["openai/gpt-oss-120b"]
