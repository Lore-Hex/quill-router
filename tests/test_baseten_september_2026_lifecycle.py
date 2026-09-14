from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import baseten
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 9, 26, 0, tzinfo=UTC)
_RETIRING = {
    "z-ai/glm-4.7": "zai-org/GLM-4.7",
    "moonshotai/kimi-k2.7-code": "moonshotai/Kimi-K2.7-Code",
    "moonshotai/kimi-k2.6": "moonshotai/Kimi-K2.6",
    "thinkingmachines/inkling-1m": "thinkingmachines/inkling",
    "thinkingmachines/inkling-small": "thinkingmachines/inkling-small",
    "deepseek/deepseek-v4-pro": "deepseek-ai/DeepSeek-V4-Pro",
}
_RETAINED = {
    "z-ai/glm-5.2": "zai-org/GLM-5.2",
    "z-ai/glm-5.2-fast": "zai-org/GLM-5.2-Fast",
    "z-ai/glm-5.3": "zai-org/GLM-5.3",
    "z-ai/glm-5.3-flash": "zai-org/GLM-5.3-Flash",
    "z-ai/glm-5.3-fast": "zai-org/GLM-5.3-Fast",
    "moonshotai/kimi-k3": "moonshotai/Kimi-K3",
    "deepseek/deepseek-v4-pro-0813": "deepseek-ai/DeepSeek-V4-Pro-0813",
    "deepseek/deepseek-v4-flash-0731": "deepseek-ai/DeepSeek-V4-Flash-0731",
}


def test_baseten_september_cutoff_matches_pacific_notice() -> None:
    announced = datetime(2026, 9, 25, 17, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert announced.astimezone(UTC) == _CUTOFF
    assert provider_lifecycle.BASETEN_SEPTEMBER_2026_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING.items())
def test_baseten_september_exact_cutoff_and_provider_scope(
    model_id: str, native_id: str,
) -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("baseten", model_id, native_id, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("baseten", model_id, at=_CUTOFF)
    assert retired("baseten", "other-canonical-id", native_id, at=_CUTOFF)
    assert retired("baseten", model_id, native_id, at=_CUTOFF + timedelta(days=1))
    assert not retired("novita", model_id, native_id, at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING.items())
def test_baseten_september_runtime_cutoff_without_catalog_reload(
    monkeypatch: pytest.MonkeyPatch, model_id: str, native_id: str,
) -> None:
    # Keep the clock test independent of live discovery and earlier retirements.
    endpoints = {}
    for provider in ("baseten", "novita"):
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
    after = catalog.endpoints_for_model(model_id)
    assert len(after) == 2
    assert {endpoint.provider for endpoint in after} == {"novita"}
    assert {endpoint.model_id for endpoint in after} == {model_id}


@pytest.mark.parametrize(("model_id", "native_id"), _RETAINED.items())
def test_baseten_september_preserves_unannounced_models(model_id: str, native_id: str) -> None:
    assert not provider_lifecycle.provider_model_retired("baseten", model_id, native_id, at=_CUTOFF)


def test_baseten_september_refresh_cannot_restore_retired_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="baseten", source="api", fetched_url=baseten.URL,
        prices={model: ModelPrice(1_000_000, 2_000_000) for model in _RETIRING | _RETAINED},
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert set(refresh._index_provider_prices({"baseten": result})) == set(_RETIRING | _RETAINED)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert set(refresh._index_provider_prices({"baseten": result})) == set(_RETAINED)


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_baseten_september_stale_feed_cannot_rediscover_retired_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    payload = {"data": [
        {"id": native, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
        for native in (_RETIRING | _RETAINED).values()
    ]}
    monkeypatch.setattr(
        baseten.httpx, "HTTPTransport",
        lambda **_: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(baseten, "UPSTREAM_ID_MAP", dict(baseten.UPSTREAM_ID_MAP))
    monkeypatch.setattr(baseten, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(baseten, "MANIFEST_PATH", tmp_path / "baseten.json")
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    result = baseten.fetch()
    expected = set(_RETAINED) if after_cutoff else set(_RETIRING | _RETAINED)
    assert set(result.prices) == expected
    assert set(baseten._DISCOVERED_MANIFEST_ROWS) == expected
    baseten.write_provider_manifest(result)
    rows = json.loads(baseten.MANIFEST_PATH.read_text())["models"]
    assert {row["id"] for row in rows} == expected


def test_baseten_september_manifest_records_only_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(baseten.MANIFEST_PATH.read_text())["models"]}
    for model_id, native_id in _RETIRING.items():
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == "2026-09-26T00:00:00Z"
    for model_id in _RETAINED:
        assert "retirement_at" not in rows[model_id]
