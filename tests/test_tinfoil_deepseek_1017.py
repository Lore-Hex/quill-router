"""Tinfoil's in-place upgrade must expose 1017 without breaking undated callers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.providers import tinfoil
from tests.pinned_manifests import build_manifest_rows
from trusted_router.catalog import endpoint_confidential_compute, endpoint_e2ee

BASE = "deepseek/deepseek-v4.1-flash"
DATED = BASE + "-1017"
NATIVE = "deepseek-v4-1-flash"


@pytest.fixture
def catalog_row() -> dict:
    return {
        "id": NATIVE,
        "name": "DeepSeek V4.1 Flash",
        "description": "DeepSeek V4.1 Flash (1017), an efficient MoE model",
        "type": "chat",
        "context_window": 1_048_576,
        "endpoints": ["/v1/chat/completions", "/v1/responses"],
        "reasoning": True,
        "tool_calling": True,
        "multimodal": True,
        "pricing": {
            "inputTokenPricePer1M": "0.65",
            "cachedInputTokenPricePer1M": "0.13",
            "outputTokenPricePer1M": "1.45",
            "requestPrice": 0,
        },
    }


def test_revision_discovery_keeps_undated_callers_and_confidential_route(
    monkeypatch, tmp_path: Path, catalog_row: dict,
) -> None:
    path = tmp_path / "tinfoil.json"
    path.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", path)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": [catalog_row]})
    result = tinfoil.fetch()
    assert result.prices[BASE] == result.prices[DATED]
    assert tinfoil.UPSTREAM_ID_MAP[DATED] == NATIVE
    tinfoil.write_provider_manifest(result)
    rows = json.loads(path.read_text())["models"]
    assert {row["id"] for row in rows} == {BASE, DATED}
    _, endpoints = build_manifest_rows(monkeypatch, tmp_path / "catalog", "tinfoil", rows)
    for model in (BASE, DATED):
        endpoint = endpoints[f"{model}@tinfoil/prepaid"]
        assert endpoint.upstream_id == NATIVE
        assert endpoint.input_modalities == ("text", "image")
        assert endpoint_confidential_compute(endpoint)
        assert endpoint_e2ee(endpoint)
        assert endpoint.prompt_price_microdollars_per_million_tokens == 685_750
        assert endpoint.completion_price_microdollars_per_million_tokens == 1_529_750
        assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 137_150
    dated = next(row for row in rows if row["id"] == DATED)
    assert dated["context_length"] == 1_048_576
    assert dated["input_modalities"] == ["text", "image"]
    assert dated["endpoints"] == ["chat/completions", "responses"]


@pytest.mark.parametrize("description", [
    None, 1017, "", "DeepSeek V4.1 Flash", "DeepSeek V4.1 Flash (1101), newer model",
    "DeepSeek V4.1 Flash (1101), successor to 1017",
    "DeepSeek V4.1 Flash (1017)-experimental",
])
def test_missing_or_changed_revision_immediately_holds_existing_dated_route(
    monkeypatch, tmp_path: Path, catalog_row: dict, description: object,
) -> None:
    path = tmp_path / "tinfoil.json"
    path.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", path)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": [catalog_row]})
    tinfoil.write_provider_manifest(tinfoil.fetch())
    catalog_row["description"] = description
    result = tinfoil.fetch()
    assert BASE in result.prices
    assert DATED not in result.prices
    assert DATED not in tinfoil._DISCOVERED_MANIFEST_ROWS
    tinfoil.write_provider_manifest(result)
    rows = json.loads(path.read_text())["models"]
    dated = next(row for row in rows if row["id"] == DATED)
    assert dated["routable"] is False
    assert dated["routable_reason"] == "upstream-revision-unconfirmed"
    _, endpoints = build_manifest_rows(monkeypatch, tmp_path / "catalog", "tinfoil", rows)
    assert f"{DATED}@tinfoil/prepaid" not in endpoints
    assert f"{BASE}@tinfoil/prepaid" in endpoints


@pytest.mark.parametrize("change", [
    {"id": "unrelated-model"},
    {"pricing": {}},
    {"pricing": {"inputTokenPricePer1M": "0.65", "outputTokenPricePer1M": "1.45", "requestPrice": 1}},
])
def test_revision_alias_requires_supported_priced_native_endpoint(
    monkeypatch, catalog_row: dict, change: dict,
) -> None:
    catalog_row.update(change)
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _: {"data": [catalog_row]})
    assert DATED not in tinfoil.fetch().prices
    assert DATED not in tinfoil._DISCOVERED_MANIFEST_ROWS
