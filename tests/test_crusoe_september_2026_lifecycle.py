from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import crusoe
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 9, 13, 4, tzinfo=UTC)
_RETIRING = (
    ("z-ai/glm-5.1", "zai/GLM-5.1", "z-ai/glm-5.3"),
    ("z-ai/glm-5.2", "zai/GLM-5.2", "z-ai/glm-5.3"),
    ("deepseek/deepseek-v3-0324", "deepseek-ai/DeepSeek-V3-0324", "deepseek/deepseek-v4-pro"),
    ("qwen/qwen3-235b-a22b-2507", "Qwen/Qwen3-235B-A22B-Instruct-2507", "deepseek/deepseek-v4-flash"),
    ("meta-llama/llama-3.3-70b-instruct", "meta-llama/Llama-3.3-70B-Instruct", "deepseek/deepseek-v4-flash"),
)
_REPLACEMENTS = {
    "z-ai/glm-5.3": "zai/GLM-5.3",
    "deepseek/deepseek-v4-pro": "deepseek-ai/DeepSeek-V4-Pro",
    "deepseek/deepseek-v4-flash": "deepseek-ai/Deepseek-V4-Flash",
}


def test_crusoe_cutoff_matches_announced_pacific_time() -> None:
    announced = datetime(2026, 9, 12, 21, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert announced.astimezone(UTC) == _CUTOFF
    assert provider_lifecycle.CRUSOE_SEPTEMBER_2026_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize(("model_id", "native_id", "replacement"), _RETIRING)
def test_crusoe_exact_cutoff_and_provider_scope(
    model_id: str, native_id: str, replacement: str,
) -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("crusoe", model_id, native_id, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("crusoe", model_id, at=_CUTOFF)
    assert retired("crusoe", "other-canonical-id", native_id, at=_CUTOFF)
    assert not retired("novita", model_id, native_id, at=_CUTOFF)
    assert not retired("crusoe", replacement, _REPLACEMENTS[replacement], at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id", "_replacement"), _RETIRING)
def test_crusoe_runtime_cutoff_without_catalog_reload(
    monkeypatch: pytest.MonkeyPatch, model_id: str, native_id: str, _replacement: str,
) -> None:
    # Isolate the clock gate from independently changing discovery manifests.
    endpoints = {}
    for provider in ("crusoe", "novita"):
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


def test_crusoe_refresh_cannot_restore_retired_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    retiring = {model for model, _native, _replacement in _RETIRING}
    result = ProviderPricingResult(
        slug="crusoe", source="api", fetched_url=crusoe.URL,
        prices={model: ModelPrice(1_000_000, 2_000_000) for model in retiring | _REPLACEMENTS.keys()},
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    before = refresh._index_provider_prices({"crusoe": result})
    assert retiring <= before.keys()
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"crusoe": result})
    assert set(after) == set(_REPLACEMENTS)


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_crusoe_parser_and_manifest_cannot_rediscover_retired_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    native_ids = [native for _model, native, _replacement in _RETIRING] + list(_REPLACEMENTS.values())
    payload = {"data": [
        {"id": native, "pricing": {"prompt": "1.40", "completion": "4.40", "input_cache_reads": "0.26"}}
        for native in native_ids
    ]}
    monkeypatch.setattr(
        crusoe.httpx, "HTTPTransport",
        lambda **_: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(crusoe, "UPSTREAM_ID_MAP", dict(crusoe.UPSTREAM_ID_MAP))
    monkeypatch.setattr(crusoe, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(crusoe, "_LIVE_CANARY_OK", True)
    monkeypatch.setattr(crusoe, "MANIFEST_PATH", tmp_path / "crusoe.json")
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    result = crusoe.fetch()
    expected = set(_REPLACEMENTS)
    if not after_cutoff:
        expected.update(model for model, _native, _replacement in _RETIRING)
    assert set(result.prices) == expected
    assert set(crusoe._DISCOVERED_MANIFEST_ROWS) == expected
    for model_id, native_id, _replacement in _RETIRING:
        if not after_cutoff:
            assert crusoe._DISCOVERED_MANIFEST_ROWS[model_id]["upstream_id"] == native_id
    crusoe.write_provider_manifest(result)
    rows = json.loads(crusoe.MANIFEST_PATH.read_text())["models"]
    assert {row["id"] for row in rows} == expected


def test_crusoe_manifest_records_announced_migrations() -> None:
    rows = {row["id"]: row for row in json.loads(crusoe.MANIFEST_PATH.read_text())["models"]}
    for model_id, native_id, replacement in _RETIRING:
        assert rows[model_id]["upstream_id"] == native_id
        assert rows[model_id]["retirement_at"] == "2026-09-13T04:00:00Z"
        assert rows[model_id]["replacement_model_id"] == replacement


def test_crusoe_retirement_does_not_match_unannounced_models() -> None:
    for model_id in ("z-ai/glm-5.3-flash", "deepseek/deepseek-v3.2", "openai/gpt-oss-120b"):
        assert not provider_lifecycle.provider_model_retired("crusoe", model_id, at=_CUTOFF)
