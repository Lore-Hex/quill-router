from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import openai
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 10, 23, tzinfo=UTC)
_RETIRING = {
    "openai/gpt-3.5-turbo-0125": "gpt-3.5-turbo-0125",
    "openai/gpt-4-0613": "gpt-4-0613",
    "openai/gpt-4-turbo": "gpt-4-turbo",
    "openai/gpt-4.1-nano": "gpt-4.1-nano",
    "openai/gpt-4o-2024-05-13": "gpt-4o-2024-05-13",
}
_RETAINED = {
    "openai/gpt-4.1": "gpt-4.1",
    "openai/gpt-4.1-mini": "gpt-4.1-mini",
    "openai/gpt-4o": "gpt-4o",
    "openai/gpt-4o-2024-08-06": "gpt-4o-2024-08-06",
    "openai/o1": "o1",
    "openai/o3-mini": "o3-mini",
    "openai/o4-mini": "o4-mini",
}


def test_openai_october_cutoff_is_midnight_utc() -> None:
    assert provider_lifecycle.OPENAI_OCTOBER_23_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize(("model", "native"), _RETIRING.items())
def test_openai_snapshot_routed_before_cutover_retired_after(
    monkeypatch: pytest.MonkeyPatch, model: str, native: str,
) -> None:
    retired = provider_lifecycle.provider_model_retired
    assert not retired("openai", model, native, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("openai", model, at=_CUTOFF)
    assert retired("openai", "unknown-canonical", native, at=_CUTOFF)
    assert not retired("azure", model, native, at=_CUTOFF)
    endpoints = {}
    for slug in ("openai", "unaffected-provider"):
        endpoint = ModelEndpoint(
            id=f"{model}@{slug}/Credits", model_id=model, provider=slug,
            usage_type="Credits", upstream_id=native,
        )
        endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(model)) == 2
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert [row.provider for row in catalog.endpoints_for_model(model)] == ["unaffected-provider"]


@pytest.mark.parametrize(("model", "native"), _RETAINED.items())
def test_openai_unnamed_models_and_aliases_survive(model: str, native: str) -> None:
    assert not provider_lifecycle.provider_model_retired("openai", model, native, at=_CUTOFF)


def test_openai_refresh_cannot_restore_retired_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ProviderPricingResult(
        slug="openai", source="api", fetched_url=openai.URL,
        prices={model: ModelPrice(100_000, 200_000) for model in _RETIRING | _RETAINED},
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert set(refresh._index_provider_prices({"openai": result})) == set(_RETAINED)


def test_openai_manifest_records_the_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(openai.MANIFEST_PATH.read_text())["models"]}
    for model in ("openai/gpt-3.5-turbo-0125", "openai/gpt-4-0613", "openai/gpt-4.1-nano"):
        assert rows[model]["retirement_at"] == "2026-10-23T00:00:00Z"
    assert "retirement_at" not in rows["openai/gpt-4.1-mini"]
