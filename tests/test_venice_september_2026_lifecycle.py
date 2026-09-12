from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.providers import venice
from tests.lifecycle_clock import catalog_predates
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 9, 15, tzinfo=UTC)
_OLD = "deepseek/deepseek-v4-flash"
_NEW = "deepseek/deepseek-v4-1-flash"


def test_venice_retirement_boundary_and_scope() -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("venice", _OLD, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("venice", _OLD, at=_CUTOFF)
    assert retired("venice", "another-id", "deepseek-v4-flash", at=_CUTOFF)
    assert not retired("novita", _OLD, "deepseek-v4-flash", at=_CUTOFF)
    for model in (_NEW, _OLD + "-0731", _OLD + "-0731-fast", "z-ai/glm-5.3-flash"):
        assert not retired("venice", model, model.split("/", 1)[1], at=_CUTOFF)


def test_venice_runtime_cutoff_never_substitutes_new_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_endpoints = [
        ModelEndpoint(
            id=f"{_OLD}@{provider}/prepaid", model_id=_OLD, provider=provider,
            upstream_id="deepseek-v4-flash", usage_type="Credits",
        ) for provider in ("venice", "novita")
    ]
    replacement = ModelEndpoint(
        id=f"{_NEW}@venice/prepaid", model_id=_NEW, provider="venice",
        upstream_id="deepseek-v4-1-flash", usage_type="Credits",
    )
    monkeypatch.setattr(
        catalog, "MODEL_ENDPOINTS", {e.id: e for e in [*old_endpoints, replacement]},
    )
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1),
    )
    assert catalog.endpoints_for_model(_OLD) == old_endpoints
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert catalog.endpoints_for_model(_OLD) == [old_endpoints[1]]
    assert catalog.endpoints_for_model(_NEW) == [replacement]


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_venice_discovery_respects_retirement_and_preserves_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    now = _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: now)
    monkeypatch.setenv("VENICE_API_KEY", "test-key")
    monkeypatch.setattr(venice, "UPSTREAM_ID_MAP", dict(venice.UPSTREAM_ID_MAP))
    monkeypatch.setattr(venice, "_DISCOVERED_MANIFEST_ROWS", {})
    payload = {"data": [
        {
            "id": native,
            "model_spec": {
                "name": native, "offline": False,
                "availableContextTokens": 1_000_000,
                "maxCompletionTokens": 131_072,
                "pricing": {
                    "input": {"usd": "0.375"}, "output": {"usd": "1.5"},
                    "cache_input": {"usd": "0.0075"},
                },
                "capabilities": {
                    "supportsVision": native == "deepseek-v4-1-flash",
                    "supportsFunctionCalling": True,
                    "supportsReasoning": True,
                    "supportsResponseSchema": True,
                },
            },
        } for native in ("deepseek-v4-flash", "deepseek-v4-1-flash", "zai-org-glm-5-2")
    ]}
    monkeypatch.setattr(venice, "fetch_json", lambda *args, **kwargs: payload)
    result = venice.fetch()
    assert (_OLD in result.prices) is not after_cutoff
    assert (_OLD in venice._DISCOVERED_MANIFEST_ROWS) is not after_cutoff
    price = result.prices[_NEW]
    assert price.prompt_micro_per_m == 375_000
    assert price.completion_micro_per_m == 1_500_000
    assert price.tiers[0].prompt_cached_micro_per_m == 7_500
    new_row = venice._DISCOVERED_MANIFEST_ROWS[_NEW]
    assert new_row["upstream_id"] == "deepseek-v4-1-flash"
    assert new_row["input_modalities"] == ["text", "image"]
    assert set(new_row["features"]) == {"function-calling", "reasoning", "structured-outputs"}
    assert new_row["context_length"] == 1_000_000
    assert new_row["max_output_tokens"] == 131_072

    manifest_path = tmp_path / "venice.json"
    manifest_path.write_text(json.dumps({"provider": "venice", "models": [{
        "id": _OLD, "upstream_id": "deepseek-v4-flash",
        "retirement_at": "2026-09-15T00:00:00Z", "replacement_model_id": _NEW,
    }]}))
    monkeypatch.setattr(venice, "MANIFEST_PATH", manifest_path)
    venice.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(manifest_path.read_text())["models"]}
    assert rows[_NEW]["upstream_id"] == "deepseek-v4-1-flash"
    assert rows[_NEW]["input_token_price_per_m"] == 375_000
    if not after_cutoff:
        assert rows[_OLD]["retirement_at"] == "2026-09-15T00:00:00Z"
        assert rows[_OLD]["replacement_model_id"] == _NEW
    # A result fetched before retirement cannot reintroduce the price afterward.
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert _OLD not in refresh._index_provider_prices({"venice": result})


def test_venice_committed_catalog_records_migration() -> None:
    rows = {row["id"]: row for row in json.loads(venice.MANIFEST_PATH.read_text())["models"]}
    assert rows[_OLD]["retirement_at"] == "2026-09-15T00:00:00Z"
    assert rows[_OLD]["replacement_model_id"] == _NEW
    assert rows[_OLD]["upstream_id"] == "deepseek-v4-flash"
    assert rows[_NEW]["upstream_id"] == "deepseek-v4-1-flash"
    assert (f"{_OLD}@venice/prepaid" in catalog.MODEL_ENDPOINTS) is catalog_predates(_CUTOFF)
    endpoint = catalog.MODEL_ENDPOINTS[f"{_NEW}@venice/prepaid"]
    assert endpoint.upstream_id == "deepseek-v4-1-flash"
