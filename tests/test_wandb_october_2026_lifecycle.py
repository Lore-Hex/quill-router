from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.providers import _direct_openai, wandb
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 10, 5, tzinfo=UTC)
_RETIRING = {
    "deepseek/deepseek-v4-pro": "deepseek-ai/DeepSeek-V4-Pro",
    "deepseek/deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash",
    "ibm-granite/granite-4.1-8b": "ibm-granite/granite-4.1-8b",
    "jetbrains/mellum2-12b-a2.5b-instruct": "JetBrains/Mellum2-12B-A2.5B-Instruct",
    "meta-llama/llama-3.1-70b-instruct": "meta-llama/Llama-3.1-70B-Instruct",
    "openpipe/qwen3-14b-instruct": "OpenPipe/Qwen3-14B-Instruct",
    "qwen/qwen3.6-27b": "Qwen/Qwen3.6-27B",
    "qwen/qwen3.5-35b-a3b": "Qwen/Qwen3.5-35B-A3B",
    "qwen/qwen3-30b-a3b-instruct-2507": "Qwen/Qwen3-30B-A3B-Instruct-2507",
}
_RETAINED = {
    "deepseek/deepseek-v4-pro-0813": "deepseek-ai/DeepSeek-V4-Pro-0813",
    "deepseek/deepseek-v4-flash-0731": "deepseek-ai/DeepSeek-V4-Flash-0731",
    "ibm-granite/granite-4.2-8b": "ibm-granite/granite-4.2-8b",
    "meta-llama/llama-3.3-70b-instruct": "meta-llama/Llama-3.3-70B-Instruct",
    "meta-llama/llama-3.1-8b-instruct": "meta-llama/Llama-3.1-8B-Instruct",
    "qwen/qwen3.8-27b": "Qwen/Qwen3.8-27B",
    "qwen/qwen3.6-35b-a3b": "Qwen/Qwen3.6-35B-A3B",
    "z-ai/glm-5.3-flash": "zai-org/GLM-5.3-Flash",
    "z-ai/glm-5.2": "zai-org/GLM-5.2",
    "minimax/minimax-m3": "MiniMaxAI/MiniMax-M3",
}


def test_wandb_cutoff_is_conservative_for_date_only_notice() -> None:
    assert provider_lifecycle.WANDB_OCTOBER_2026_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING.items())
def test_wandb_retirement_boundary_and_provider_scope(model_id: str, native_id: str) -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("wandb", model_id, native_id, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("wandb", model_id, at=_CUTOFF)
    assert retired("wandb", "different-canonical-id", native_id, at=_CUTOFF)
    assert retired("wandb", model_id, native_id, at=_CUTOFF + timedelta(days=1))
    assert not retired("unaffected-provider", model_id, native_id, at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING.items())
def test_wandb_runtime_cutoff_without_reload_or_weight_substitution(
    monkeypatch: pytest.MonkeyPatch, model_id: str, native_id: str,
) -> None:
    endpoints = {}
    for provider in ("wandb", "unaffected-provider"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{model_id}@{provider}/{usage}", model_id=model_id,
                provider=provider, usage_type=usage, upstream_id=native_id,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(model_id)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    remaining = catalog.endpoints_for_model(model_id)
    assert len(remaining) == 2
    assert {endpoint.provider for endpoint in remaining} == {"unaffected-provider"}
    assert {endpoint.model_id for endpoint in remaining} == {model_id}
    assert {endpoint.upstream_id for endpoint in remaining} == {native_id}


@pytest.mark.parametrize(("model_id", "native_id"), _RETAINED.items())
def test_wandb_unannounced_models_survive(model_id: str, native_id: str) -> None:
    assert not provider_lifecycle.provider_model_retired("wandb", model_id, native_id, at=_CUTOFF)


@pytest.mark.parametrize("source", ["api", "snapshot"])
def test_wandb_refresh_cannot_restore_retired_prices(
    monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    result = ProviderPricingResult(
        slug="wandb", source=source, fetched_url=wandb.URL,
        prices={model: ModelPrice(100_000, 200_000) for model in _RETIRING | _RETAINED},
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert set(refresh._index_provider_prices({"wandb": result})) == set(_RETIRING | _RETAINED)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert set(refresh._index_provider_prices({"wandb": result})) == set(_RETAINED)


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_wandb_stale_discovery_does_not_probe_or_republish_retired_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    monkeypatch.setenv("WANDB_API_KEY", "test-key")
    monkeypatch.setattr(wandb, "_load_model_docs", lambda: {})
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: (
        _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1)
    ))
    probes = []

    def probe(**kwargs: object) -> bool:
        probes.append(kwargs["model"])
        return True

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    models = _RETIRING | _RETAINED
    discovery = DirectOpenAIProvider(
        replace(
            wandb.CATALOG.spec,
            catalog_loader=lambda _key: [{"id": native} for native in models.values()],
            price_loader=lambda: {model: ModelPrice(100_000, 200_000) for model in models},
        ),
        manifest_path=tmp_path / "wandb.json",
    )
    expected = _RETAINED if after_cutoff else models
    result = discovery.fetch()
    discovery.write_provider_manifest(result)
    assert set(result.prices) == set(expected)
    assert set(discovery.discovered_rows) == set(expected)
    assert set(probes) == set(expected.values())
    rows = json.loads(discovery.manifest_path.read_text())["models"]
    assert {row["id"]: row["upstream_id"] for row in rows} == expected


def test_wandb_manifest_records_only_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(wandb.MANIFEST_PATH.read_text())["models"]}
    assert {model for model, row in rows.items() if "retirement_at" in row} == set(_RETIRING)
    for model_id, native_id in _RETIRING.items():
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == "2026-10-05T00:00:00Z"
    for model_id in _RETAINED:
        assert "retirement_at" not in rows[model_id]


def test_wandb_refresh_preserves_retirement_annotations(tmp_path: Path) -> None:
    manifest_path = tmp_path / "wandb.json"
    manifest_path.write_text(wandb.MANIFEST_PATH.read_text())
    rows = json.loads(manifest_path.read_text())["models"]
    discovered = {
        row["id"]: {key: value for key, value in row.items() if key != "retirement_at"}
        for row in rows
    }
    result = ProviderPricingResult(
        slug="wandb", source="api", fetched_url=wandb.URL,
        prices={row["id"]: ModelPrice(100_000, 200_000) for row in rows},
    )
    write_discovered_chat_manifest(
        result, manifest_path=manifest_path, discovered_rows=discovered, source_url=wandb.URL,
    )
    refreshed = {row["id"]: row for row in json.loads(manifest_path.read_text())["models"]}
    for model_id in _RETIRING:
        assert refreshed[model_id]["retirement_at"] == "2026-10-05T00:00:00Z"
