from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import near_ai
from tests.lifecycle_clock import catalog_predates
from tests.test_near_ai_provider import _catalog_row, _endpoints
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 9, 11, 13, 0, tzinfo=UTC)
_DEEPSEEK_CUTOFF = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)
_DSV4 = "deepseek-ai/DeepSeek-V4-Flash"
_RETIRING = (
    ("z-ai/glm-5.1", "zai-org/GLM-5.1-FP8", _CUTOFF),
    ("z-ai/glm-5.2", "z-ai/glm-5.2", _CUTOFF),
    ("deepseek/deepseek-v4-flash", _DSV4, _DEEPSEEK_CUTOFF),
)
_SURVIVOR = "google/gemma-4-31B-it"
_SURVIVOR_MODEL = "google/gemma-4-31b-it"


@pytest.mark.parametrize(("model_id", "native_id", "cutoff"), _RETIRING)
def test_near_ai_exact_cutoff_and_provider_scope(
    model_id: str, native_id: str, cutoff: datetime,
) -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("near-ai", model_id, native_id, at=cutoff - timedelta(microseconds=1))
    assert retired("near-ai", model_id, at=cutoff)
    assert retired("near-ai", "other-canonical-id", native_id, at=cutoff)
    assert not retired("deepinfra", model_id, native_id, at=cutoff)
    assert not retired("near-ai", "z-ai/glm-5.3-flash", at=cutoff)
    assert not retired("near-ai", model_id + "-other", at=cutoff)


@pytest.mark.parametrize(("model_id", "native_id", "cutoff"), _RETIRING)
def test_near_ai_runtime_cutoff_without_catalog_reload(
    monkeypatch: pytest.MonkeyPatch, model_id: str, native_id: str, cutoff: datetime,
) -> None:
    endpoints = {
        provider: ModelEndpoint(
            id=f"{model_id}@{provider}/prepaid", model_id=model_id,
            provider=provider, upstream_id=native_id, usage_type="Credits",
        ) for provider in ("near-ai", "deepinfra")
    }
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", {e.id: e for e in endpoints.values()})
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: cutoff - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(model_id)) == 2
    assert near_ai.canonical_model_id(native_id) == model_id
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: cutoff)
    assert catalog.endpoints_for_model(model_id) == [endpoints["deepinfra"]]
    assert near_ai.canonical_model_id(native_id) is None


def test_near_ai_imported_catalog_matches_cutoff() -> None:
    rows = {row["id"]: row for row in json.loads(near_ai.MANIFEST_PATH.read_text())["models"]}
    for model_id, _native_id, cutoff in _RETIRING:
        expected = catalog_predates(cutoff) and rows[model_id].get("routable") is not False
        assert (f"{model_id}@near-ai/prepaid" in catalog.MODEL_ENDPOINTS) is expected


@pytest.mark.parametrize("after_cutoff", [False, True])
@pytest.mark.parametrize("price_rows_present", [False, True])
@pytest.mark.parametrize("cutoff", [_CUTOFF, _DEEPSEEK_CUTOFF])
def test_near_ai_refresh_cutoff_and_missing_price_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    after_cutoff: bool,
    price_rows_present: bool,
    cutoff: datetime,
) -> None:
    now = cutoff if after_cutoff else cutoff - timedelta(microseconds=1)
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: now,
    )
    retiring_ids = [native for _model, native, _cutoff in _RETIRING]
    # Missing prices hold only the affected row; retirement removes it even if
    # the stale direct registry and price API still advertise it.
    replacement = "z-ai/glm-5.3-flash"
    rows = [_catalog_row(_SURVIVOR), _catalog_row(replacement), _catalog_row("unreviewed/model")]
    rows.extend(
        _catalog_row(native) for _model, native, model_cutoff in _RETIRING
        if price_rows_present or model_cutoff != cutoff
    )
    endpoints = _endpoints(_SURVIVOR, *retiring_ids)
    endpoints["endpoints"].append({
        "domain": "glm-5-3-flash.completions.near.ai", "models": ["z-ai/glm-5.3-flash"],
    })

    def respond(request: httpx.Request) -> httpx.Response:
        payload = {"data": rows} if str(request.url) == near_ai.CATALOG_URL else endpoints
        return httpx.Response(200, json=payload)

    monkeypatch.setenv("NEAR_API_KEY", "test-key")
    monkeypatch.setattr(near_ai.httpx, "HTTPTransport", lambda **_: httpx.MockTransport(respond))
    monkeypatch.setattr(near_ai, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(near_ai, "UPSTREAM_ID_MAP", dict(near_ai.UPSTREAM_ID_MAP))
    monkeypatch.setattr(near_ai, "MANIFEST_PATH", tmp_path / "near-ai.json")
    monkeypatch.setattr(near_ai, "_OPERATOR_HOLD_REASONS", {})
    monkeypatch.setattr(near_ai, "_VERIFIED_DIRECT_MODELS", {
        native: pair for native, pair in near_ai._VERIFIED_DIRECT_MODELS.items()
        if native in {*retiring_ids, _SURVIVOR, replacement}
    })

    result = near_ai.fetch()
    expected = {_SURVIVOR_MODEL, replacement} | {
        model for model, _native, model_cutoff in _RETIRING if now < model_cutoff
    }
    missing = {
        model for model, _native, model_cutoff in _RETIRING
        if now < model_cutoff and not price_rows_present and model_cutoff == cutoff
    }
    assert set(result.prices) == expected - missing
    assert set(near_ai._DISCOVERED_MANIFEST_ROWS) == expected
    for model_id, native_id, model_cutoff in _RETIRING:
        if now < model_cutoff:
            assert near_ai._DISCOVERED_MANIFEST_ROWS[model_id]["upstream_id"] == native_id
    assert "unreviewed/model" not in result.prices
    near_ai.write_provider_manifest(result)
    manifest = json.loads(near_ai.MANIFEST_PATH.read_text())
    assert {row["id"] for row in manifest["models"]} == expected
    for row in manifest["models"]:
        if row["id"] in missing:
            assert row["routable"] is False
            assert row["routable_reason"] == "price-unavailable"
            assert "input_token_price_per_m" not in row


def test_near_ai_stale_price_result_cannot_restore_retired_deepseek(monkeypatch) -> None:
    model = "deepseek/deepseek-v4-flash"
    result = ProviderPricingResult(
        slug="near-ai", source="api", fetched_url=near_ai.CATALOG_URL,
        prices={m: ModelPrice(170_000, 350_000) for m in (model, _SURVIVOR_MODEL)},
    )
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now", lambda: _DEEPSEEK_CUTOFF - timedelta(microseconds=1),
    )
    assert model in refresh._index_provider_prices({"near-ai": result})
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _DEEPSEEK_CUTOFF)
    assert set(refresh._index_provider_prices({"near-ai": result})) == {_SURVIVOR_MODEL}


def test_near_ai_manifest_records_deepseek_migration() -> None:
    rows = json.loads(near_ai.MANIFEST_PATH.read_text())["models"]
    row = next(row for row in rows if row["id"] == "deepseek/deepseek-v4-flash")
    assert row["upstream_id"] == _DSV4
    assert row["retirement_at"] == "2026-09-17T13:00:00Z"
    assert row["replacement_model_id"] == "z-ai/glm-5.3-flash"
